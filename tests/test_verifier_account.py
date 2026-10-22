"""Witnesses for running verifiers as the dedicated account (Issue #9).

These need the account from scripts/setup_verifier_user.sh. The macOS CI job creates
it and sets CAIRNWORK_REQUIRE_VERIFIER_ACCOUNT=1, so they cannot be skipped there.
"""

import os
import shutil
import subprocess
import sys
import time

import pytest
from conftest import commit_change, git

import cairnwork.verifiers as verifiers
from cairnwork.journal import Journal
from cairnwork.notary import Notary
from cairnwork.ownership import OwnershipLedger
from cairnwork.recovery import recover
from cairnwork.verifier_account import VerifierAccount, VerifierAccountError
from cairnwork.verifiers import parse_verifier, run_verifiers

pytestmark = [pytest.mark.sandbox, pytest.mark.verifier_account]
ACCOUNT = VerifierAccount()


@pytest.fixture(autouse=True)
def _clean_shared_run_root():
    """The run root is shared: remove what these tests leave there."""
    before = set(os.listdir(ACCOUNT.run_root)) if ACCOUNT.run_root.is_dir() else set()
    yield
    if not ACCOUNT.run_root.is_dir():
        return
    for name in set(os.listdir(ACCOUNT.run_root)) - before:
        ACCOUNT.remove(ACCOUNT.run_root / name)
        shutil.rmtree(ACCOUNT.run_root / name, ignore_errors=True)


@pytest.fixture
def project(repo):
    commit_change(
        repo,
        {
            "tests/conftest.py": (
                "import sys, pathlib\n"
                "sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / 'src'))\n"
            ),
            "tests/test_ok.py": "def test_ok():\n    assert 1 + 1 == 2\n",
        },
    )
    return repo


def verify(project, files, target, monkeypatch=None, extra_env=()):
    head = commit_change(project, files) if files else git(project, "rev-parse", "HEAD")
    if monkeypatch is not None and extra_env:
        monkeypatch.setattr(verifiers, "_ENV_KEEP", (*verifiers._ENV_KEEP, *extra_env))
    spec = parse_verifier("pytest", ["python3", "-m", "pytest", target])
    run_id = "t" + os.urandom(6).hex()
    return run_verifiers(project, head, (spec,), ACCOUNT.run_root, run_id, account=ACCOUNT)[0]


def test_pytest_runs_as_the_verifier_account(project):
    code = f"import os\ndef test_uid():\n    assert os.getuid() != {os.getuid()}\n"
    result = verify(project, {"tests/test_uid.py": code}, "tests/test_uid.py")
    assert result.observation.passed, result.log_path.read_text()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS KERN_PROCARGS2; Linux uses /proc")
def test_owner_process_arguments_are_not_readable(project, monkeypatch):
    secret = "CWFAKESECRET" + os.urandom(4).hex()
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", secret])
    monkeypatch.setenv("CW_PID", str(owner.pid))
    monkeypatch.setenv("CW_SECRET", secret)
    code = (
        "import ctypes, os\n"
        "def test_procargs():\n"
        "    libc = ctypes.CDLL('/usr/lib/libc.dylib')\n"
        "    mib = (ctypes.c_int * 3)(1, 49, int(os.environ['CW_PID']))  # KERN_PROCARGS2\n"
        "    size = ctypes.c_size_t(1 << 16)\n"
        "    buf = ctypes.create_string_buffer(size.value)\n"
        "    rc = libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0)\n"
        "    data = buf.raw[: size.value] if rc == 0 else b''\n"
        "    assert os.environ['CW_SECRET'].encode() not in data\n"
    )
    try:
        result = verify(
            project,
            {"tests/test_procargs.py": code},
            "tests/test_procargs.py",
            monkeypatch,
            ("CW_PID", "CW_SECRET"),
        )
    finally:
        owner.kill()
        owner.wait()
    assert result.observation.passed, result.log_path.read_text()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux /proc; macOS uses sysctl")
def test_owner_process_is_not_reachable_from_the_sandbox(project, monkeypatch):
    # The Linux counterpart of the macOS process-args witness. The property is the same — the
    # candidate cannot read the owner's process secrets — but the mechanism is the bwrap PID
    # namespace, not the uid: the owner's host pid is not in the sandbox's namespace, so its
    # /proc entry does not exist there (the uid boundary on the *filesystem* is witnessed
    # separately by test_owner_only_files_are_not_readable). The test asserts the entry is
    # genuinely absent, so it cannot pass vacuously on a present-but-unreadable file.
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    monkeypatch.setenv("CW_PID", str(owner.pid))
    code = (
        "import os, pytest\n"
        "def test_proc():\n"
        "    # The owner's host pid is not in the sandbox PID namespace, so its /proc entry is\n"
        "    # absent — the candidate cannot reach the owner's process args or environment.\n"
        "    with pytest.raises(FileNotFoundError):\n"
        "        open('/proc/' + os.environ['CW_PID'] + '/environ', 'rb').read()\n"
    )
    try:
        result = verify(
            project, {"tests/test_proc.py": code}, "tests/test_proc.py", monkeypatch, ("CW_PID",)
        )
    finally:
        owner.kill()
        owner.wait()
    assert result.observation.passed, result.log_path.read_text()


def test_owner_only_files_are_not_readable_where_the_sandbox_allows_reading(project, monkeypatch):
    # The shared venv is inside the sandbox's read roots, so only the uid can stop a read.
    tag = os.urandom(4).hex()
    private = ACCOUNT.root / "venv" / f"cw-owner-only-{tag}"
    shared = ACCOUNT.root / "venv" / f"cw-group-{tag}"
    for path, mode in ((private, 0o600), (shared, 0o640)):
        path.write_text("x")
        os.chmod(path, mode)
    monkeypatch.setenv("CW_PRIVATE", str(private))
    monkeypatch.setenv("CW_SHARED", str(shared))
    code = (
        "import os, pytest\n"
        "def test_read():\n"
        "    assert open(os.environ['CW_SHARED']).read() == 'x'\n"
        "    with pytest.raises(PermissionError):\n"
        "        open(os.environ['CW_PRIVATE']).read()\n"
    )
    try:
        result = verify(
            project,
            {"tests/test_read.py": code},
            "tests/test_read.py",
            monkeypatch,
            ("CW_PRIVATE", "CW_SHARED"),
        )
    finally:
        private.unlink()
        shared.unlink()
    assert result.observation.passed, result.log_path.read_text()


def test_escaped_process_is_killed(project):
    import cairnwork_sandbox

    code = (
        "import subprocess, sys\n"
        "def test_spawn():\n"
        "    subprocess.Popen([sys.executable, '-P', '-c', 'import time; time.sleep(30)'],"
        " start_new_session=True, env={})\n"
    )
    result = verify(project, {"tests/test_daemon.py": code}, "tests/test_daemon.py")
    assert result.returncode == 0  # pytest itself passed
    assert ACCOUNT.count() == 0  # the 30 s sleeper is gone on either backend, not left running
    # Seatbelt cannot reap an escaped session, so confined.py sweeps the survivor by run token
    # and fails the run; under bwrap the escapee stays in the PID namespace and is killed on
    # teardown, so there is nothing left to sweep and the run may pass (docs/SECURITY.md).
    backend = cairnwork_sandbox.platform_backend()
    if backend is not None and backend.name == "seatbelt":
        assert not result.observation.passed


def test_unusable_account_fails_closed(project):
    bad = VerifierAccount(user="cairnwork-no-such-user")
    spec = parse_verifier("pytest", ["python3", "-m", "pytest", "tests/test_ok.py"])
    with pytest.raises(VerifierAccountError):
        run_verifiers(
            project, git(project, "rev-parse", "HEAD"), (spec,), bad.run_root, "r1", account=bad
        )


def test_remove_refuses_paths_outside_the_run_root(tmp_path):
    with pytest.raises(VerifierAccountError):
        ACCOUNT.remove(tmp_path)


def test_files_the_account_leaves_in_home_are_removed(project):
    code = (
        "import os\n"
        "def test_leave():\n"
        "    private = os.path.join(os.environ['HOME'], 'private')\n"
        "    os.makedirs(private, mode=0o700)\n"
        "    open(os.path.join(private, 'f'), 'w').write('x')\n"
    )
    result = verify(project, {"tests/test_leave.py": code}, "tests/test_leave.py")
    assert result.observation.passed, result.log_path.read_text()
    assert not (result.log_path.parent / "run" / "home").exists()


def _account_pids_in_group(pgid: int) -> list[int]:
    """PIDs the verifier account runs in process group `pgid` — the orphan started with
    start_new_session=True is that group's leader, so this sees only this test's own tree and
    concurrent runs on the shared account cannot change the result (Issue #71). pgid/pgid are
    visible across users (unlike argv), so no marker is needed."""
    tty = ["-x"] if sys.platform == "darwin" else []  # BSD ps needs -x; procps rejects it
    done = subprocess.run(  # noqa: S603 - fixed argv
        ["/bin/ps", *tty, "-U", ACCOUNT.user, "-o", "pid=,pgid="],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    out = []
    for line in done.stdout.split("\n"):
        fields = line.split()
        if len(fields) == 2 and fields[1].isdigit() and int(fields[1]) == pgid:
            out.append(int(fields[0]))
    return out


def _wait_for_account_group(pgid: int) -> None:
    for _ in range(50):
        if _account_pids_in_group(pgid):
            return
        time.sleep(0.1)
    raise AssertionError("the account orphan did not start")


def test_recovery_kills_what_a_dead_run_left_as_the_account(tmp_path):
    orphan = subprocess.Popen(
        ACCOUNT.wrap([ACCOUNT.python, "-P", "-c", "import time; time.sleep(60)"], None),
        cwd=ACCOUNT.root,
        start_new_session=True,
    )
    group = os.getpgid(orphan.pid)
    _wait_for_account_group(group)
    recover(
        ledger=OwnershipLedger(tmp_path / "own.sqlite"),
        notary=Notary(tmp_path / "notary.sqlite"),
        journal=Journal(tmp_path / "journal.sqlite"),
        run_root=tmp_path / "runs",
        verifier_account=ACCOUNT,
    )
    orphan.wait(
        timeout=10
    )  # recover ended our tree (a live sleep(60) would time out — fails closed)
    assert _account_pids_in_group(group) == []  # none of our account processes remain


def test_a_left_over_process_is_ended_before_the_next_run(project):
    orphan = subprocess.Popen(
        ACCOUNT.wrap([ACCOUNT.python, "-P", "-c", "import time; time.sleep(60)"], None),
        cwd=ACCOUNT.root,
        start_new_session=True,
    )
    _wait_for_account_group(os.getpgid(orphan.pid))
    result = verify(project, None, "tests/test_ok.py")
    assert result.observation.passed, result.log_path.read_text()  # not blamed on this run
    assert orphan.wait(timeout=10) != 0


def test_the_owner_itself_is_not_a_usable_account():
    import pwd

    assert not VerifierAccount(user=pwd.getpwuid(os.getuid()).pw_name).available()


def test_an_unknown_account_count_is_unknown_not_zero():
    assert VerifierAccount(user="cairnwork-no-such-user").count() == -1


def test_a_strict_caller_umask_does_not_break_the_account_run(project):
    # Issue #91: grant_read must open the checkout to the account whatever the umask — on Linux the
    # clone's group is the owner's (setgid is stripped from the run work dir), so a strict umask
    # leaves it unreadable to the account unless read is granted to *other*. Runs on both platforms.
    old = os.umask(0o077)  # e.g. a Gateway service started with umask 077
    try:
        result = verify(project, None, "tests/test_ok.py")
    finally:
        os.umask(old)
    assert result.observation.passed, result.log_path.read_text()


def test_tests_can_run_git_in_the_checkout_they_do_not_own(project):
    """The account does not own the checkout; git in it must still work (Nexus-new #2760
    pilot: 'detected dubious ownership'), and only that checkout is trusted.

    The whole observation must pass, including on a full-Xcode host (macOS CI): there
    /usr/bin/git goes through xcodebuild and launchd starts the account's
    /usr/sbin/distnoted, which cairnwork#37 now exempts from the escaped-process count (by
    its real path + ppid 1 + Apple signature), so a harmless launchd daemon no longer fails
    the run. Hosts with only the Command Line Tools never start it.
    """
    code = (
        "import os, subprocess\n"
        "def test_git():\n"
        "    git = '/usr/bin/git'\n"
        # Ignore the host's system/global git config: a CI runner may set safe.directory=* in
        # /etc/gitconfig, which is the runner's doing, not the verifier's. Only the per-run
        # value Cairnwork injects (GIT_CONFIG_*) should decide what this run trusts.
        "    env = dict(os.environ, GIT_CONFIG_SYSTEM='/dev/null', GIT_CONFIG_GLOBAL='/dev/null')\n"
        "    done = subprocess.run([git, 'rev-parse', 'HEAD'], capture_output=True, env=env)\n"
        "    assert done.returncode == 0, done.stderr\n"
        "    out = subprocess.run([git, 'config', '--get-all', 'safe.directory'],\n"
        "                         capture_output=True, text=True, env=env).stdout.split()\n"
        "    assert out and '*' not in out\n"
    )
    result = verify(project, {"tests/test_git.py": code}, "tests/test_git.py")
    log = result.log_path.read_text()
    assert result.outcomes == {"tests/test_git.py::test_git": "passed"}, log
    assert result.returncode == 0 and not result.timed_out and result.mutated == (), log
    assert result.observation.passed, log  # cairnwork#37: launchd's distnoted is not an escape


def test_account_may_write_next_to_the_repo_but_not_move_the_checkout(project):
    """cairnwork#38 under the account: the sticky work directory takes new files, but the
    checkout (owner's) cannot be renamed away or replaced."""
    code = (
        "import os\n"
        "from pathlib import Path\n"
        "repo = Path(__file__).resolve().parents[1]\n"
        "def test_layout():\n"
        "    (repo.parent / 'nexus-runtime-targets').mkdir()\n"
        "    try:\n"
        "        os.rename(repo, repo.parent / 'moved')\n"
        "    except PermissionError:\n"
        "        return\n"
        "    raise AssertionError('moved the checkout')\n"
    )
    result = verify(project, {"tests/test_layout.py": code}, "tests/test_layout.py")
    log = result.log_path.read_text()
    assert result.outcomes == {"tests/test_layout.py::test_layout": "passed"}, log
    assert result.mutated == (), log
    clone_id = result.log_path.parent.name.split(".")[0]
    assert not (ACCOUNT.run_root / f"{clone_id}.work").exists(), "work directory left behind"


def test_account_files_without_permissions_are_still_removed(project):
    """The account can leave a directory it closed to itself; cleanup reopens it first."""
    code = (
        "import os\n"
        "from pathlib import Path\n"
        "def test_lock():\n"
        "    locked = Path(__file__).resolve().parents[1].parent / 'locked'\n"
        "    (locked / 'inner').mkdir(parents=True)\n"
        "    (locked / 'inner' / 'f').write_text('x')\n"
        "    os.chmod(locked / 'inner', 0)\n"
        "    os.chmod(locked, 0)\n"
    )
    result = verify(project, {"tests/test_lock.py": code}, "tests/test_lock.py")
    assert result.outcomes == {"tests/test_lock.py::test_lock": "passed"}, (
        result.log_path.read_text()
    )
    clone_id = result.log_path.parent.name.split(".")[0]
    assert not (ACCOUNT.run_root / f"{clone_id}.work").exists()


def test_account_cannot_swap_its_scratch_directories(project):
    """run/ is sticky: the account cannot rename the owner's home/ away (to plant a symlink
    the owner's cleanup would follow)."""
    code = (
        "import os\n"
        "def test_swap_home():\n"
        "    home = os.environ['HOME']\n"
        "    try:\n"
        "        os.rename(home, home + '.x')\n"
        "    except PermissionError:\n"
        "        return\n"
        "    raise AssertionError('renamed home')\n"
    )
    result = verify(project, {"tests/test_home.py": code}, "tests/test_home.py")
    assert result.outcomes == {"tests/test_home.py::test_swap_home": "passed"}, (
        result.log_path.read_text()
    )


def test_remove_does_not_follow_a_symlink(tmp_path):
    target = ACCOUNT.run_root / ("keep-" + os.urandom(4).hex())
    link = ACCOUNT.run_root / ("link-" + os.urandom(4).hex())
    target.mkdir()
    os.chmod(target, 0o2770)  # the account could delete what is inside
    (target / "evidence").write_text("keep")
    try:
        link.symlink_to(target)
        ACCOUNT.remove(link)
        assert (target / "evidence").exists()
    finally:
        link.unlink(missing_ok=True)
        (target / "evidence").unlink(missing_ok=True)
        target.rmdir()
