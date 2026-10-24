"""local-worker-tool's two git queries (status, numstat), run sandboxed in direct mode (#88)."""

import hashlib
import subprocess

import pytest
from local_worker_fakes import TAG, WRAPPER, make_runs
from local_worker_fakes import run_wrapper as run


@pytest.fixture
def runs(tmp_path):
    return make_runs(tmp_path)


def test_git_queries_refuse_a_symlinked_git_dir(runs):
    co = runs / TAG / "checkout"
    (co / ".git").rmdir()
    (co / ".git").symlink_to(runs / ("f" * 32))
    for action in ("status", "numstat"):
        proc = run(runs, action, str(runs / TAG))
        assert proc.returncode == 64 and b".git" in proc.stderr, action


@pytest.mark.sandbox
def test_git_queries_observe_the_tree_without_writing_git(runs):
    """status/numstat run sandboxed, read-only on .git: a real repo, changed by the 'worker'."""
    from experimental.local_worker.gitstate import parse_numstat, parse_status

    co = runs / TAG / "checkout"
    (co / ".git").rmdir()
    git = ["git", "-C", str(co), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], check=True)
    (co / ".gitignore").write_text("*.log\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    head = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout
    root = str(runs / TAG)
    clean = run(runs, "status", root)
    assert clean.returncode == 0, clean.stdout
    tree = parse_status(clean.stdout)
    assert tree.observed and tree.head == head.strip() and tree.entries == ()
    before = _git_snapshot(co)
    assert run(runs, "write", root, "a.txt", stdin=b"hello\nworld\n").returncode == 0
    assert run(runs, "write", root, "new dir/b.py", stdin=b"x\ny\nz\n").returncode == 0
    assert run(runs, "write", root, "x.log", stdin=b"l\n").returncode == 0
    tree = parse_status(run(runs, "status", root).stdout)
    assert tree.paths() == ["a.txt", "new dir/b.py", "x.log"]
    stat = run(runs, "numstat", root)
    # staged with --force: the ignored x.log counts too (every file the run changed)
    assert stat.returncode == 0 and parse_numstat(stat.stdout) == (5, 0), stat.stdout
    assert _git_snapshot(co) == before  # .git never written: same paths, modes and bytes
    assert list((runs / TAG / "tmp").iterdir()) == []  # the queries never write tmp
    assert list((runs / TAG / "q").iterdir()) == []  # and remove their throwaway index from q


def _git_checkout(runs):
    co = runs / TAG / "checkout"
    (co / ".git").rmdir()
    git = ["git", "-C", str(co), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], check=True)
    (co / ".gitignore").write_text("*.log\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    return co, str(runs / TAG)


@pytest.mark.sandbox
def test_numstat_ignores_planted_git_attributes_and_ignore_files(runs):
    """Review of #239, finding 2: files the worker can write must not change what numstat counts.
    $HOME/.config/git/* is ignored (HOME is /var/empty for git); a worktree .gitattributes "-diff"
    or .gitignore "*" is itself part of the change, but cannot hide or binarise other lines."""
    from experimental.local_worker.gitstate import parse_numstat

    co, root = _git_checkout(runs)
    assert run(runs, "write", root, "a.txt", stdin=b"hello\nworld\n").returncode == 0
    assert run(runs, "write", root, "new/b.py", stdin=b"x\ny\nz\n").returncode == 0
    base = parse_numstat(run(runs, "numstat", root).stdout)
    assert base == (4, 0)
    home = runs / TAG / "tmp"  # the sandboxed HOME of every other action
    (home / ".config" / "git").mkdir(parents=True)
    (home / ".config" / "git" / "attributes").write_text("* -diff\n")
    (home / ".config" / "git" / "ignore").write_text("*\n")
    (home / ".gitconfig").write_text("[core]\n\tattributesFile = ~/.config/git/attributes\n")
    assert parse_numstat(run(runs, "numstat", root).stdout) == base
    assert run(runs, "write", root, ".gitattributes", stdin=b"* -diff\n").returncode == 0
    assert parse_numstat(run(runs, "numstat", root).stdout) == (5, 0)  # +1: the file itself
    assert run(runs, "write", root, "sub/.gitignore", stdin=b"*\n").returncode == 0
    assert parse_numstat(run(runs, "numstat", root).stdout) == (6, 0)  # nothing hidden


def test_git_query_temp_dir_is_made_by_mktemp():
    """Review of #239, finding 4: no predictable name in the worker-writable tmp."""
    text = WRAPPER.read_text()
    assert '/usr/bin/mktemp -d "$TMPDIR/gitq.XXXXXXXXXX"' in text
    assert "$$" not in text[text.index("GIT_QUERY=") : text.index("real_git()")]


@pytest.mark.sandbox
def test_git_queries_fail_on_an_unreadable_directory(runs):
    """Review of #239, finding 3: git skips a directory it cannot open with a warning on stderr
    and exit 0; the query must fail (not observed), never report a tree without it."""
    co, root = _git_checkout(runs)
    assert run(runs, "status", root).returncode == 0
    hidden = co / "hidden"
    hidden.mkdir()
    (hidden / "f.py").write_text("x\n")
    hidden.chmod(0)
    try:
        for action in ("status", "numstat"):
            proc = run(runs, action, root)
            assert proc.returncode in (3, 4), (action, proc.returncode, proc.stdout)
            assert b"hidden" in proc.stdout  # git's message instead of a result
    finally:
        hidden.chmod(0o700)
    assert list((runs / TAG / "q").iterdir()) == []  # temp dir removed on failure too


def _git_snapshot(co):
    """Every path under .git with its mode and the sha256 of its bytes (not just the names)."""
    out = {}
    for p in sorted((co / ".git").rglob("*")):
        st = p.lstat()
        data = p.read_bytes() if p.is_file() and not p.is_symlink() else b""
        out[str(p.relative_to(co))] = (st.st_mode, hashlib.sha256(data).hexdigest())
    return out


@pytest.mark.sandbox
def test_git_queries_run_no_configured_filter_and_leave_git_unchanged(runs):
    """Codex review of #239: a clean/smudge/process filter configured in .git/config, in a
    config file it includes, or named by a worktree .gitattributes never runs during status or
    numstat, which count the bytes as they are; .git keeps every byte and mode."""
    from experimental.local_worker.gitstate import parse_numstat, parse_status

    co, root = _git_checkout(runs)
    git = ["git", "-C", str(co), "-c", "user.email=t@t", "-c", "user.name=t"]
    (co / "t.py").write_text("base1\n")  # tracked; the change keeps its size: status must hash it
    subprocess.run([*git, "add", "t.py"], check=True)
    subprocess.run([*git, "commit", "-qm", "t"], check=True)
    tmp = runs / TAG / "q"  # the one place a query may write: a filter that ran would mark here
    hook = "/usr/bin/touch {m}; /bin/cat"
    included = co / "included.cfg"  # an include target the worker could have written
    included.write_text(f'[filter "evil2"]\n\tclean = {hook.format(m=tmp / "m2")}\n')
    with (co / ".git" / "config").open("a") as fh:
        fh.write(f'[filter "evil"]\n\tclean = {hook.format(m=tmp / "m1")}\n')
        fh.write(
            f"\tsmudge = {hook.format(m=tmp / 'm1s')}\n\tprocess = /usr/bin/touch {tmp / 'm1p'}\n"
        )
        fh.write("\trequired = true\n")
        fh.write(f"[include]\n\tpath = {included}\n")
    (co / ".git" / "info").mkdir(exist_ok=True)
    (co / ".git" / "info" / "attributes").write_text("*.py filter=evil\n")
    (co / ".git" / "info" / "exclude").write_text("*.tmp\n")
    before = _git_snapshot(co)
    assert run(runs, "write", root, "a.py", stdin=b"one\ntwo\n").returncode == 0
    assert run(runs, "write", root, ".gitattributes", stdin=b"*.txt filter=evil2\n").returncode == 0
    assert run(runs, "write", root, "b.txt", stdin=b"three\n").returncode == 0
    assert run(runs, "write", root, "t.py", stdin=b"base2\n").returncode == 0
    assert run(runs, "write", root, "x.tmp", stdin=b"t\n").returncode == 0
    tree = parse_status(run(runs, "status", root).stdout)
    want = [".gitattributes", "a.py", "b.txt", "included.cfg", "t.py", "x.tmp"]
    assert tree.observed and tree.paths() == want
    assert "! x.tmp" in tree.entries  # the real info/exclude still applies
    stat = run(runs, "numstat", root)
    assert stat.returncode == 0 and parse_numstat(stat.stdout) == (8, 1), stat.stdout
    assert list(tmp.iterdir()) == []  # no marker, no temp dir
    assert _git_snapshot(co) == before


def test_git_query_writes_only_new_files_in_its_mktemp_dir():
    """Codex review of #239, B2: no mkdir -p, no cp onto a path, noclobber before any write."""
    text = WRAPPER.read_text()
    query = text[text.index("GIT_QUERY=") : text.index("real_git()")]
    assert "mkdir -p" not in query and "/bin/cp " not in query
    assert query.index("set -C") < query.index('>"$q/out"') < query.index('>"$o/HEAD"')
