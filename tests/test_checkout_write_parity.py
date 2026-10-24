"""Issue #240: under the verifier account a verifier may write its checkout as in owner mode.

The same five candidate tests run in both modes and must give the same verdicts (``EXPECTED``):
writing and cleaning up passes; a leftover file, a renamed ``.git``, a symlink swapped in, or a
directory left without permissions is "checkout changed". Every work dir is removed afterwards,
including what the account wrote. The account run is one ``run_verifiers`` call (one lock hold).
``grant_write`` itself is checked on a plain tree: bits, no symlink followed, hard links stay
read-only, bottom-up order.
"""

import os
import shutil
import stat
import subprocess
import sys

import pytest
from conftest import commit_change
from test_recovery import GOOD, crash, stores
from test_recovery import project as project  # noqa: F401 - fixture

from cairnwork import checkout_grant
from cairnwork.recovery import recover
from cairnwork.verifier_account import VerifierAccount
from cairnwork.verifiers import parse_verifier, run_verifiers

ACCOUNT = VerifierAccount()

_HEAD = "import os, shutil\nfrom pathlib import Path\nROOT = Path(__file__).resolve().parents[1]\n"
SCENARIOS = {
    # what an honest test does: a fixture under tests/examples, an in-place rewrite restored, an
    # mtime touch (a template cache keyed by mtime), a cache file removed again
    "clean": (
        "def test_clean():\n"
        "    examples = ROOT / 'tests' / 'examples' / 'new'\n"
        "    examples.mkdir(parents=True)\n"
        "    (examples / 'fixture.txt').write_text('x')\n"
        "    shutil.rmtree(ROOT / 'tests' / 'examples')\n"
        "    data = ROOT / 'pkg' / 'data.txt'\n"
        "    text = data.read_text()\n"
        "    data.write_text('changed\\n')\n"
        "    data.write_text(text)\n"
        "    os.utime(data, None)\n"
        "    (ROOT / 'pkg' / '.cache').write_text('c')\n"
        "    (ROOT / 'pkg' / '.cache').unlink()\n"
    ),
    "leave": "def test_leave():\n    (ROOT / 'pkg' / 'left.txt').write_text('x')\n",
    "gitmove": "def test_gitmove():\n    os.rename(ROOT / '.git', ROOT / '.git-moved')\n",
    "swap": (
        "def test_swap():\n"
        "    os.rename(ROOT / 'pkg' / 'sub', ROOT / 'pkg' / 'sub.real')\n"
        "    os.symlink('sub.real', ROOT / 'pkg' / 'sub')\n"
        "    (ROOT / 'pkg' / 'data.txt').unlink()\n"
        "    os.symlink('/etc/hosts', ROOT / 'pkg' / 'data.txt')\n"
    ),
    "locked": (
        "def test_locked():\n"
        "    locked = ROOT / 'tests' / 'locked'\n"
        "    (locked / 'inner').mkdir(parents=True)\n"
        "    (locked / 'inner' / 'f').write_text('x')\n"
        "    os.chmod(locked / 'inner', 0)\n"
        "    os.chmod(locked, 0)\n"
    ),
}
# (passed, what mutated must contain): identical for both modes
EXPECTED = {
    "clean": (True, set()),
    "leave": (False, {"extra pkg/left.txt"}),
    "gitmove": (False, {"extra directory .git-moved"}),
    "swap": (False, {"changed pkg/sub", "changed pkg/data.txt", "extra directory pkg/sub.real"}),
    "locked": (False, {"unreadable tests/locked"}),
}


def _normal(entry: str) -> str:
    """'unreadable <absolute path>' names a path inside the run's checkout: keep the tail."""
    if entry.startswith("unreadable ") and "/tests/" in entry:
        return "unreadable tests/" + entry.split("/tests/", 1)[1]
    return entry


def _run_scenarios(repo, run_root, account, run_id):
    files = {f"tests/test_{name}.py": _HEAD + body for name, body in SCENARIOS.items()}
    files.update({"pkg/__init__.py": "", "pkg/data.txt": "v1\n", "pkg/sub/x.txt": "x\n"})
    head = commit_change(repo, files)
    specs = tuple(
        parse_verifier(name, ["python3", "-m", "pytest", f"tests/test_{name}.py"])
        for name in SCENARIOS
    )
    python = None if account is not None else sys.executable
    runs = run_verifiers(repo, head, specs, run_root, run_id, python=python, account=account)
    for run in runs:
        assert run.ran, run.log_path.read_text()  # the guard saw the test run and pass
    assert not list(run_root.glob(f"{run_id}-v*.work")), "a work directory was left behind"
    return {run.spec.verifier_id: run for run in runs}


def _check(runs):
    for name, (passed, must_contain) in EXPECTED.items():
        run = runs[name]
        log = run.log_path.read_text()
        assert run.returncode == 0, (name, log)  # every candidate test itself succeeded
        assert run.observation.passed is passed, (name, run.mutated, log)
        assert must_contain <= {_normal(m) for m in run.mutated}, (name, run.mutated)
        assert ("checkout changed" in run.why) is not passed, (name, run.why)


@pytest.mark.sandbox
def test_owner_mode_checkout_writes(repo, tmp_path):
    _check(_run_scenarios(repo, tmp_path / "runs", None, "w" + os.urandom(6).hex()))


@pytest.fixture
def own_names():
    """Name prefixes this test creates in the account's run root; only those are removed after
    it. The run root is shared with other sessions' runs: never remove "whatever is new". If a
    failure left owner directories shut (0o700), reopen them first so the account's rm gets in."""
    prefixes: list[str] = []
    yield prefixes
    for name in os.listdir(ACCOUNT.run_root):
        if not any(name.startswith(prefix) for prefix in prefixes):
            continue
        path = ACCOUNT.run_root / name
        for top, dirs, _ in os.walk(path):
            for entry in [top, *(os.path.join(top, d) for d in dirs)]:
                if not os.path.islink(entry) and os.lstat(entry).st_uid == os.getuid():
                    os.chmod(entry, os.lstat(entry).st_mode | 0o777)
        ACCOUNT.remove(path)
        shutil.rmtree(path, ignore_errors=True)


@pytest.mark.sandbox
@pytest.mark.verifier_account
def test_account_mode_checkout_writes_match_owner_mode(repo, own_names):
    """Parity: the same EXPECTED table as owner mode, as the dedicated account."""
    run_id = "w" + os.urandom(6).hex()
    own_names.append(f"{run_id}-")
    _check(_run_scenarios(repo, ACCOUNT.run_root, ACCOUNT, run_id))


@pytest.mark.sandbox
@pytest.mark.verifier_account
def test_recovery_removes_what_the_account_wrote_in_a_crashed_checkout(
    project, tmp_path, own_names
):
    """A crash leaves a writable checkout holding a directory the account closed to itself.
    Recovery removes all of it, even when the owner-side pass covers the same root: the account
    goes first, before the owner's chmod 0o700 could shut it out."""
    crash(project, tmp_path, "CANDIDATE", GOOD)
    ledger, notary, journal = stores(tmp_path)
    attempt = journal.entries(ledger.in_flight()[0].task_id)[-1].attempt_id
    own_names.append(f"{attempt}-")
    work = ACCOUNT.run_root / f"{attempt}-q-v0.work"
    work.mkdir(mode=0o700)
    os.chmod(work, 0o3770)  # as verifiers._run_all makes it
    checkout = work / f"{attempt}-q-v0"
    (checkout / "tests").mkdir(parents=True)
    (checkout / "tests" / "test_x.py").write_text("x\n")
    ACCOUNT.grant_write(checkout)
    locked = checkout / "tests" / "locked"
    code = (
        f"import os; p = {str(locked)!r}; os.makedirs(p + '/inner');"
        " open(p + '/inner/f', 'w').write('x'); os.chmod(p + '/inner', 0); os.chmod(p, 0)"
    )
    with ACCOUNT.exclusive():
        done = subprocess.run(  # noqa: S603 - fixed argv
            ACCOUNT.wrap([ACCOUNT.python, "-P", "-c", code], None),
            cwd=ACCOUNT.root, capture_output=True, text=True, timeout=60, check=False,
        )  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert os.lstat(locked).st_uid != os.getuid()  # the account's, closed to the owner
    (decision,) = recover(
        ledger=ledger, notary=notary, journal=journal, run_root=ACCOUNT.run_root,
        verifier_account=ACCOUNT,
    )  # fmt: skip
    assert decision.detail == "", decision.detail
    assert not os.path.lexists(work)


def _tree(root):
    (root / "sub" / "deep").mkdir(parents=True)
    (root / "a.txt").write_text("a")
    (root / "run.sh").write_text("#!/bin/sh\n")
    os.chmod(root / "run.sh", 0o700)
    (root / "sub" / "deep" / "b.txt").write_text("b")


def test_grant_write_opens_dirs_and_files_to_group_and_other(tmp_path):
    root = tmp_path / "checkout"
    old = os.umask(0o077)
    try:
        root.mkdir()
        _tree(root)
    finally:
        os.umask(old)
    checkout_grant.grant_write(root)
    mode = {p: stat.S_IMODE(os.lstat(root / p).st_mode) for p in ("", "sub", "sub/deep")}
    assert set(mode.values()) == {0o777}
    assert stat.S_IMODE(os.lstat(root / "a.txt").st_mode) == 0o666
    assert stat.S_IMODE(os.lstat(root / "sub/deep/b.txt").st_mode) == 0o666
    assert stat.S_IMODE(os.lstat(root / "run.sh").st_mode) == 0o766  # x bits stay as tracked


def test_grant_write_never_follows_a_symlink(tmp_path):
    root, outside = tmp_path / "checkout", tmp_path / "outside"
    root.mkdir(mode=0o700)
    outside.mkdir(mode=0o700)
    (outside / "secret").write_text("s")
    os.chmod(outside / "secret", 0o600)
    (root / "to-dir").symlink_to(outside)
    (root / "to-file").symlink_to(outside / "secret")
    (root / "dangling").symlink_to(tmp_path / "missing")
    checkout_grant.grant_write(root)
    assert stat.S_IMODE(os.stat(outside).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(outside / "secret").st_mode) == 0o600
    assert stat.S_IMODE(os.stat(root).st_mode) == 0o777


def test_grant_write_refuses_a_symlink_as_the_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (tmp_path / "link").symlink_to(outside)
    with pytest.raises(OSError):
        checkout_grant.grant_write(tmp_path / "link")
    assert stat.S_IMODE(os.stat(outside).st_mode) == 0o700


def test_grant_write_keeps_a_hard_linked_file_read_only(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    (tmp_path / "elsewhere").write_text("e")
    os.chmod(tmp_path / "elsewhere", 0o600)
    os.link(tmp_path / "elsewhere", root / "linked")
    checkout_grant.grant_write(root)
    assert stat.S_IMODE(os.stat(tmp_path / "elsewhere").st_mode) == 0o644  # read, never write


def test_grant_write_is_bottom_up(tmp_path, monkeypatch):
    """A directory is opened to the account only after everything inside it is done."""
    root = tmp_path / "checkout"
    root.mkdir()
    _tree(root)
    order = []
    real_fchmod = os.fchmod

    def record(fd, mode):
        order.append(os.fstat(fd).st_ino)
        real_fchmod(fd, mode)

    monkeypatch.setattr(os, "fchmod", record)
    checkout_grant.grant_write(root)
    position = {inode: index for index, inode in enumerate(order)}
    for top, dirs, files in os.walk(root):
        parent = position[os.lstat(top).st_ino]
        for name in dirs + files:
            assert position[os.lstat(os.path.join(top, name)).st_ino] < parent, (top, name)
