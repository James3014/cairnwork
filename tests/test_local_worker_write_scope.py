"""The optional write scope (#88 follow-up): local-worker-tool ``write --scope`` in direct mode,
and the tool layer that passes it (write_scope.py)."""

import os
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest
from local_worker_fakes import TAG, WRAPPER, make_runs
from local_worker_fakes import run_wrapper as run

from experimental.local_worker import write_scope
from experimental.local_worker.tools import WorkerTools, subprocess_invoker


@pytest.fixture
def runs(tmp_path):
    return make_runs(tmp_path)


def write(runs, path, *scope, data=b"new\n"):
    extra = ("--scope", *scope) if scope else ()
    return run(runs, "write", str(runs / TAG), path, *extra, stdin=data)


def refused(proc) -> bool:
    return proc.returncode == 64 and b"refusing" in proc.stderr and proc.stdout == b""


@pytest.mark.sandbox
@pytest.mark.parametrize(
    ("path", "scope"),
    [
        ("a.txt", ("a.txt",)),  # an exact file
        ("sub/new.py", ("sub",)),  # under a directory
        ("sub/deep/er/new.py", ("sub",)),  # directories mkdir creates, under the entry
        ("pkg/mod/new.py", ("pkg/mod/new.py",)),  # its missing ancestors may be created
        ("sub/x.py", ("other", "sub/x.py")),  # any entry
    ],
)
def test_a_write_in_scope_lands(runs, path, scope):
    proc = write(runs, path, *scope)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (runs / TAG / "checkout" / path).read_bytes() == b"new\n"


@pytest.mark.parametrize(
    ("path", "scope"),
    [
        ("a.txt", ("sub",)),  # outside every entry
        ("sub/x.py", ("sub/y.py",)),  # a file entry is not its directory
        ("subx/f.py", ("sub",)),  # a prefix only at a "/" boundary
        ("su", ("sub",)),
        ("sub/x.py", ("sub/X.py",)),  # byte for byte, as K's match
    ],
)
def test_a_write_out_of_scope_is_refused_and_writes_nothing(runs, path, scope):
    before = sorted(p.relative_to(runs) for p in runs.rglob("*"))
    proc = write(runs, path, *scope)
    assert refused(proc) and b"out of write scope" in proc.stderr, proc.stderr
    assert sorted(p.relative_to(runs) for p in runs.rglob("*")) == before
    assert (runs / TAG / "checkout" / "a.txt").read_text() == "hello\n"


@pytest.mark.parametrize(
    "path", ["sub/../a.txt", "../" + TAG + "/checkout/a.txt", "sub/./x", "sub//x", "-x"]
)
def test_dot_dot_and_odd_paths_never_reach_the_scope_check(runs, path):
    assert refused(write(runs, path, "sub", "a.txt"))
    assert (runs / TAG / "checkout" / "a.txt").read_text() == "hello\n"


def test_an_absolute_path_is_refused_even_inside_the_scope(runs):
    target = runs / TAG / "checkout" / "sub" / "x.py"
    assert refused(write(runs, str(target), "sub"))
    assert not target.exists()


@pytest.mark.parametrize("entry", ["../x", "/abs", "a/./b", "a//b", ".git", "sub/.GIT/x", "-n"])
def test_a_bad_scope_entry_refuses_the_call(runs, entry):
    assert refused(write(runs, "a.txt", entry))
    assert (runs / TAG / "checkout" / "a.txt").read_text() == "hello\n"


def test_scope_is_only_for_write_and_needs_an_entry(runs):
    root = str(runs / TAG)
    assert refused(run(runs, "read", root, "a.txt", "--scope", "a.txt"))
    assert refused(run(runs, "write", root, "a.txt", "--scope", stdin=b"x"))
    assert (runs / TAG / "checkout" / "a.txt").read_text() == "hello\n"


def test_a_symlinked_directory_in_the_checkout_cannot_carry_a_write_out_of_scope(runs):
    co = runs / TAG / "checkout"
    (co / "out").mkdir()
    (co / "sub" / "esc").symlink_to("../out")  # under the entry, pointing out of it
    (co / "link").symlink_to("out")  # the entry itself is a symlink: taken literally
    for path, scope in (("sub/esc/x.py", "sub"), ("link/x.py", "link")):
        proc = write(runs, path, scope)
        assert refused(proc) and b"out of write scope" in proc.stderr, (path, proc.stderr)
    assert list((co / "out").iterdir()) == []


@pytest.mark.sandbox
def test_a_symlink_into_scope_lands_in_scope(runs):
    co = runs / TAG / "checkout"
    (co / "in").symlink_to("sub")
    assert write(runs, "in/x.py", "sub").returncode == 0
    assert (co / "sub" / "x.py").read_bytes() == b"new\n"


@pytest.mark.sandbox
def test_a_hard_link_in_scope_is_refused(runs):
    """Another name of the same file may lie outside the scope (test code can make one)."""
    co = runs / TAG / "checkout"
    (co / "out").mkdir()
    (co / "out" / "f").write_text("orig\n")
    os.link(co / "out" / "f", co / "sub" / "h")
    proc = write(runs, "sub/h", "sub")
    assert refused(proc) and b"out of write scope: sub/h has 2 hard links" in proc.stderr
    assert (co / "out" / "f").read_text() == "orig\n"
    (co / "out" / "f").unlink()  # its only name now: in scope
    assert write(runs, "sub/h", "sub").returncode == 0
    assert (co / "sub" / "h").read_bytes() == b"new\n"


def _case_insensitive(path: Path) -> bool:
    return (path.parent / path.name.upper()).exists()


@pytest.mark.sandbox
def test_a_case_variant_cannot_reach_an_existing_file_out_of_scope(runs):
    co = runs / TAG / "checkout"
    (co / "sub" / "X.py").write_text("keep\n")
    if not _case_insensitive(co / "a.txt"):
        pytest.skip("a case-sensitive volume: sub/x.py is another file")
    proc = write(runs, "sub/x.py", "sub/x.py")  # lands on sub/X.py: out of scope
    assert refused(proc) and b"out of write scope" in proc.stderr
    assert (co / "sub" / "X.py").read_text() == "keep\n"
    assert write(runs, "SUB/X.py", "sub/X.py").returncode == 0  # lands on sub/X.py: in scope
    assert (co / "sub" / "X.py").read_bytes() == b"new\n"


@pytest.mark.sandbox
def test_paths_are_canonical_and_an_entry_must_match_the_disk(runs):
    """The landing path takes each existing component's on-disk case; entries stay literal."""
    co = runs / TAG / "checkout"
    if not _case_insensitive(co / "a.txt"):
        pytest.skip("a case-sensitive volume: SUB is another directory")
    proc = write(runs, "SUB/n.py", "SUB")  # lands on sub/n.py: the entry SUB is not sub
    assert refused(proc) and b"out of write scope" in proc.stderr
    assert not (co / "sub" / "n.py").exists()
    assert write(runs, "SUB/n.py", "sub").returncode == 0  # lands on sub/n.py: in scope
    assert (co / "sub" / "n.py").read_bytes() == b"new\n"


@pytest.mark.sandbox
def test_an_nfd_entry_does_not_match_an_nfc_directory(runs):
    nfc, nfd = unicodedata.normalize("NFC", "caf\u00e9"), unicodedata.normalize("NFD", "caf\u00e9")
    co = runs / TAG / "checkout"
    (co / nfc).mkdir()
    if not (co / nfd).exists():
        pytest.skip("a normalization-sensitive volume: the NFD name is another directory")
    proc = write(runs, f"{nfd}/x.py", nfd)  # lands in the NFC directory: not the NFD entry
    assert refused(proc) and b"out of write scope" in proc.stderr
    assert os.listdir(co / nfc) == []
    assert write(runs, f"{nfd}/x.py", nfc).returncode == 0  # lands in the NFC entry
    assert os.listdir(co / nfc) == ["x.py"]


@pytest.mark.sandbox
def test_without_a_scope_writes_go_anywhere_in_the_checkout(runs):
    for path in ("a.txt", "sub/x.py", "new/dir/y.py"):
        assert write(runs, path).returncode == 0, path


# -- the Python side ----------------------------------------------------------------------------
def test_normalize_is_sorted_canonical_and_none_stays_none():
    assert write_scope.normalize(None) is None
    assert write_scope.normalize(["src/", "a.py", "src"]) == ("a.py", "src")
    assert write_scope.wrapper_args(None) == []
    assert write_scope.wrapper_args(("a.py",)) == ["--scope", "a.py"]


@pytest.mark.parametrize("bad", [[], ["/abs"], ["a/../b"], ["./a"], [""], [".git/x"], ['a"b']])
def test_normalize_refuses_what_the_wrapper_would(bad):
    with pytest.raises(ValueError):
        write_scope.normalize(bad)


def _tools(runs, tmp_path, scope):
    env = dict(
        os.environ,
        CAIRNWORK_LOCAL_WORKER_RUNS=str(runs),
        CAIRNWORK_LOCAL_WORKER_PYROOT=sys.base_prefix,
    )
    t = WorkerTools(
        runs / TAG,
        tmp_path / "calls.jsonl",
        mode="direct",
        wrapper=WRAPPER,
        invoker=subprocess_invoker(env),
        timeout_seconds=60,
    )
    t.write_scope = scope
    return t


@pytest.mark.sandbox
def test_tools_refuse_out_of_scope_writes_and_edits_then_go_on(runs, tmp_path):
    t = _tools(runs, tmp_path, ("sub",))
    co = runs / TAG / "checkout"
    bad_write = t.write_file("a.txt", "x\n")
    bad_edit = t.edit_file("a.txt", "hello", "bye")
    good = t.write_file("sub/ok.py", "ok\n")
    edit = t.edit_file("sub/ok.py", "ok", "fine")
    for call in (bad_write, bad_edit):
        assert call.error_category == "REFUSED" and call.exit_code == 64
        assert "out of write scope" in call.output
    assert (co / "a.txt").read_text() == "hello\n"
    assert good.error_category == edit.error_category == "NONE"
    assert (co / "sub" / "ok.py").read_text() == "fine\n"
    unscoped = _tools(runs, tmp_path, None)
    assert unscoped.write_file("a.txt", "x\n").exit_code == 0


def _without_the_check(runs, tmp_path):
    """The wrapper with its own scope check taken out: only the Seatbelt layer is left."""
    text = WRAPPER.read_text()
    check = '[ -z "$SCOPE_SET" ] || require_in_scope "$1" "$probe"'
    assert text.count(check) == 1
    copy = tmp_path / "local-worker-tool"
    copy.write_text(text.replace(check, ":"))
    env = dict(
        os.environ,
        CAIRNWORK_LOCAL_WORKER_RUNS=str(runs),
        CAIRNWORK_LOCAL_WORKER_PYROOT=sys.base_prefix,
    )

    def write_unchecked(path, *scope):
        return subprocess.run(
            ["/bin/sh", str(copy), "write", str(runs / TAG), path, "--scope", *scope],
            input=b"new\n",
            capture_output=True,
            env=env,
            cwd="/",
            check=False,
        )

    return write_unchecked


@pytest.mark.sandbox
def test_the_profile_alone_keeps_writes_in_scope(runs, tmp_path):
    """The second layer: with the wrapper's own check taken out, Seatbelt still allows writes in
    the checkout only under the entries (a symlink swapped in after the check gains nothing)."""
    write_unchecked = _without_the_check(runs, tmp_path)
    co = runs / TAG / "checkout"
    (co / "out").mkdir()
    (co / "sub" / "esc").symlink_to("../out")
    for path in ("a.txt", "subx/f.py", "sub/esc/x.py"):
        proc = write_unchecked(path, "sub")
        assert proc.returncode != 0 and b"Operation not permitted" in proc.stdout, path
    assert (co / "a.txt").read_text() == "hello\n"
    assert list((co / "out").iterdir()) == [] and not (co / "subx").exists()


@pytest.mark.sandbox
def test_the_profile_lets_an_entrys_ancestors_be_created_as_directories_only(runs, tmp_path):
    write_unchecked = _without_the_check(runs, tmp_path)
    co = runs / TAG / "checkout"
    proc = write_unchecked("pkg", "pkg/mod/new.py")  # a regular file at an ancestor path
    assert proc.returncode != 0 and not (co / "pkg").exists()
    proc = write_unchecked("pkg/zzz/f.py", "pkg/mod/new.py")  # a directory beside the ancestor
    assert proc.returncode != 0 and (co / "pkg").is_dir() and not (co / "pkg" / "zzz").exists()
    assert write_unchecked("pkg/mod/new.py", "pkg/mod/new.py").returncode == 0
    assert (co / "pkg" / "mod" / "new.py").read_bytes() == b"new\n"
