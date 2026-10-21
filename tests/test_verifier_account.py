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


def test_escaped_process_is_killed_and_fails_the_run(project):
    code = (
        "import subprocess, sys\n"
        "def test_spawn():\n"
        "    subprocess.Popen([sys.executable, '-P', '-c', 'import time; time.sleep(30)'],"
        " start_new_session=True, env={})\n"
    )
    result = verify(project, {"tests/test_daemon.py": code}, "tests/test_daemon.py")
    assert result.returncode == 0  # pytest itself passed; only the escaped process fails it
    assert not result.observation.passed
    assert ACCOUNT.count() == 0  # the 30 s sleeper is gone, not left running


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


def test_recovery_kills_what_a_dead_run_left_as_the_account(tmp_path):
    orphan = subprocess.Popen(
        ACCOUNT.wrap([ACCOUNT.python, "-P", "-c", "import time; time.sleep(60)"], None),
        cwd=ACCOUNT.root,
        start_new_session=True,
    )
    for _ in range(50):
        if ACCOUNT.count() > 0:
            break
        time.sleep(0.1)
    assert ACCOUNT.count() > 0
    recover(
        ledger=OwnershipLedger(tmp_path / "own.sqlite"),
        notary=Notary(tmp_path / "notary.sqlite"),
        journal=Journal(tmp_path / "journal.sqlite"),
        run_root=tmp_path / "runs",
        verifier_account=ACCOUNT,
    )
    assert ACCOUNT.count() == 0
    orphan.wait(timeout=10)


def test_a_left_over_process_is_ended_before_the_next_run(project):
    orphan = subprocess.Popen(
        ACCOUNT.wrap([ACCOUNT.python, "-P", "-c", "import time; time.sleep(60)"], None),
        cwd=ACCOUNT.root,
        start_new_session=True,
    )
    for _ in range(50):
        if ACCOUNT.count() > 0:
            break
        time.sleep(0.1)
    result = verify(project, None, "tests/test_ok.py")
    assert result.observation.passed, result.log_path.read_text()  # not blamed on this run
    assert orphan.wait(timeout=10) != 0


def test_the_owner_itself_is_not_a_usable_account():
    import pwd

    assert not VerifierAccount(user=pwd.getpwuid(os.getuid()).pw_name).available()


def test_an_unknown_account_count_is_unknown_not_zero():
    assert VerifierAccount(user="cairnwork-no-such-user").count() == -1


def test_a_strict_caller_umask_does_not_break_the_account_run(project):
    old = os.umask(0o077)  # e.g. a Gateway service started with umask 077
    try:
        result = verify(project, None, "tests/test_ok.py")
    finally:
        os.umask(old)
    assert result.observation.passed, result.log_path.read_text()


def test_tests_can_run_git_in_the_checkout_they_do_not_own(project, monkeypatch):
    """The account does not own the checkout; git in it must still work (Nexus-new #2760
    pilot: 'detected dubious ownership'), and only that checkout is trusted."""
    code = (
        "import subprocess\n"
        "def test_git():\n"
        "    git = '/usr/bin/git'\n"
        "    done = subprocess.run([git, 'rev-parse', 'HEAD'], capture_output=True, text=True)\n"
        "    assert done.returncode == 0, done.stderr\n"
        "    out = subprocess.run([git, 'config', '--get-all', 'safe.directory'],\n"
        "                         capture_output=True, text=True).stdout.split()\n"
        "    assert out and '*' not in out\n"
    )
    seen = []
    original_reap = VerifierAccount.reap

    def reap_and_record(self):
        ps = ["/bin/ps", "-x", "-U", self.user, "-o", "pid,ppid,comm"]
        seen.append(subprocess.run(ps, capture_output=True, text=True).stdout)
        return original_reap(self)

    monkeypatch.setattr(VerifierAccount, "reap", reap_and_record)
    result = verify(project, {"tests/test_git.py": code}, "tests/test_git.py")
    assert result.observation.passed, (result.mutated, seen, result.log_path.read_text())
