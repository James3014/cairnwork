"""#234 PR-C: the account hint and precheck, worker isolation by uid, and the Issue runner's
mirror invariant. The verify and worker paths are covered in test_uncomparable_paths.py."""

import errno
import grp
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import git
from test_pipeline import EditWorker

import cairnwork.issue_runner as runner
from cairnwork import account_check
from cairnwork.deferred_tests import BaseUncomparable
from cairnwork.intake import Intake
from cairnwork.verifier_account import AccountBusy, VerifierAccountError
from cairnwork.workers import agy
from cairnwork.workers.agy import AgyWorker
from cairnwork.workers.agy_dispatch import Dispatch
from experimental.visible_tests import VisibleTestsWorker

STRANGER = (987654, frozenset({987653}))  # a uid and groups that own nothing here


@pytest.fixture
def stranger(monkeypatch):
    monkeypatch.setattr(account_check, "_ids", lambda user: STRANGER)


@pytest.fixture
def open_root():
    """A directory every account can reach: pytest's tmp_path sits under a 0700 TMPDIR on macOS,
    which would block the stranger before the directory a test means to block."""
    root = Path(tempfile.mkdtemp(dir="/tmp")).resolve()
    root.chmod(0o755)
    yield root
    for path in root.rglob("*"):
        if path.is_dir() and not path.is_symlink():
            path.chmod(0o755)
    shutil.rmtree(root)


def _python_under(root, mode):
    home = root / "home"
    (home / "venv" / "bin").mkdir(parents=True)
    python = home / "venv" / "bin" / "python"
    python.write_text("")
    python.chmod(0o755)
    home.chmod(mode)
    return home, python


def _owner(path):
    """user:group of ``path`` (BSD gives a new file its directory's group, Linux the creator's)."""
    info = path.stat()
    return f"{pwd.getpwuid(info.st_uid).pw_name}:{grp.getgrgid(info.st_gid).gr_name}"


def test_the_hint_names_the_first_directory_the_account_cannot_traverse(open_root, stranger):
    home, python = _python_under(open_root, 0o750)
    why = account_check.blocked(str(python), "cairnwork-verifier")
    assert why == f"{home} 0750 {_owner(home)} not traversable by cairnwork-verifier"
    steps = {step["path"]: step for step in account_check.walk(str(python), "x")}
    assert steps[str(home)]["ok"] is False and steps["/"]["ok"] is True
    assert steps[str(python)]["mode"] == "0755"  # every step is reported, not just the first


def test_group_and_other_bits_decide_by_class(open_root, monkeypatch):
    home, python = _python_under(open_root, 0o750)
    gids = frozenset({home.stat().st_gid})
    monkeypatch.setattr(account_check, "_ids", lambda user: (987654, gids))
    assert account_check.blocked(str(python), "member") == ""  # group r-x
    home.chmod(0o705)  # other may enter, the group may not: a member is still refused (POSIX)
    assert account_check.blocked(str(python), "member").startswith(f"{home} 0705")


@pytest.mark.parametrize(
    ("mode", "ok"), [(0o070, False), (0o700, True)], ids=["owner-denies", "owner-allows"]
)
def test_the_owner_class_comes_before_the_group_class(open_root, monkeypatch, mode, ok):
    home, python = _python_under(open_root, 0o755)
    gids = frozenset({home.stat().st_gid})
    monkeypatch.setattr(account_check, "_ids", lambda user: (os.getuid(), gids))  # owner + member
    home.chmod(mode)  # owner bits and group bits disagree: POSIX uses the owner's
    try:
        why = account_check.blocked(str(python), "owner")
    finally:
        home.chmod(0o755)
    assert (why == "") is ok, why


def test_the_real_location_of_a_symlinked_python_is_walked(open_root, stranger):
    home, python = _python_under(open_root, 0o700)
    link_dir = open_root / "open"
    link_dir.mkdir()
    (link_dir / "python").symlink_to(python)
    why = account_check.blocked(str(link_dir / "python"), "cairnwork-verifier")
    assert why.startswith(f"{home} 0700 ")


def test_an_interpreter_the_account_cannot_execute_or_that_is_missing(open_root, stranger):
    _, python = _python_under(open_root, 0o755)
    python.chmod(0o750)
    assert account_check.blocked(str(python), "v").endswith(
        "0750 " + _owner(python) + " not executable by v"
    )
    assert "cannot be read (FileNotFoundError)" in account_check.blocked(str(open_root / "no"), "v")


def test_an_unknown_account_is_named():
    assert account_check.blocked(sys.executable, "no-such-user-c7") == (
        "account no-such-user-c7 does not exist"
    )


class FakeAccount:
    user = "cairnwork-verifier"

    def __init__(self, python, usable=False, busy=False):
        self.python, self.usable, self.busy = python, usable, busy

    @contextmanager
    def exclusive(self, timeout=None):
        if self.busy:
            raise AccountBusy(f"RESOURCE_LEASE_TIMEOUT: busy for {timeout}s")
        yield

    def available(self, python=None):
        self.probes = getattr(self, "probes", 0) + 1
        return self.usable


def test_precheck_takes_its_verdict_from_available_and_its_reason_from_the_walk(
    open_root, stranger
):
    home, python = _python_under(open_root, 0o750)
    assert account_check.precheck(None, None) is None  # no account: nothing to check
    usable: Any = FakeAccount(str(python), usable=True)
    assert account_check.precheck(usable, None) is None  # mode bits say no, available() yes
    broken: Any = FakeAccount(str(python))
    with pytest.raises(BaseUncomparable) as caught:
        account_check.precheck(broken, None)
    assert caught.value.comparison.code == "VERIFIER_ACCOUNT_UNUSABLE"
    assert f"{home} 0750" in caught.value.comparison.detail


def test_a_busy_account_is_never_waited_for_and_never_refused(open_root, stranger):
    _, python = _python_under(open_root, 0o750)  # the walk says blocked (an ACL might allow it)
    busy: Any = FakeAccount(str(python), busy=True)
    deferred = account_check.precheck(busy, None)  # one try (timeout 0), then deferred
    assert (
        deferred == "VERIFIER_ACCOUNT_ERROR: RESOURCE_LEASE_TIMEOUT: busy for 0.0s"
    )  # the run's own probe decides
    assert getattr(busy, "probes", 0) == 0


def test_an_account_without_a_lock_file_is_unusable(tmp_path):
    from cairnwork.verifier_account import VerifierAccount

    account = VerifierAccount(root=tmp_path / "missing")
    with pytest.raises(BaseUncomparable, match="lock: .*No such file"):
        account_check.precheck(account, sys.executable)


def test_an_unsafe_lock_queue_is_unusable_not_deferred(tmp_path):
    from cairnwork.verifier_account import VerifierAccount

    (tmp_path / ".verifier.queue").mkdir()
    (tmp_path / ".verifier.queue").chmod(0o777)  # writable by others: lock_queue refuses it
    with pytest.raises(BaseUncomparable, match="unsafe verifier queue directory"):
        account_check.precheck(VerifierAccount(root=tmp_path), sys.executable)


def test_base_stage_turns_only_account_errors_into_uncomparable(open_root, stranger):
    _, python = _python_under(open_root, 0o750)
    account: Any = FakeAccount(str(python))
    with pytest.raises(BaseUncomparable, match="not usable; .* not traversable"):
        with account_check.base_stage(account, None):
            raise VerifierAccountError("account cairnwork-verifier is not usable")
    with pytest.raises(OSError):
        with account_check.base_stage(account, None):
            raise OSError("not an account failure")


# Isolation: agy_run_as_user counts only when it is a uid other than root, the owner and the
# verifier account.

UIDS = {"owner": os.getuid(), "verifier": 4401, "worker": 4402, "root": 0}


@pytest.fixture
def users(monkeypatch):
    def getpwnam(name):
        if name not in UIDS:
            raise KeyError(name)
        return SimpleNamespace(pw_uid=UIDS[name])

    monkeypatch.setattr(agy.pwd, "getpwnam", getpwnam)


def _agy(run_as: Any, verifier: Any = "verifier"):
    dispatch = Dispatch("/bin/d", 1, 1, 1, run_as_user=run_as)
    return AgyWorker(dispatch=dispatch, verifier_user=verifier)


@pytest.mark.parametrize(
    ("run_as", "why"),
    [
        ("owner", "root or the owner"),
        ("root", "root or the owner"),
        ("verifier", "is the verifier account"),
        ("ghost", "does not exist"),
        (None, "agy_run_as_user is not set"),
    ],
)
def test_a_worker_sharing_a_uid_is_not_isolated(users, run_as, why):
    worker = _agy(run_as)
    assert worker.isolated is False and why in worker.not_isolated_because
    assert AgyWorker().isolated is False


def test_a_worker_with_its_own_uid_is_isolated(users):
    assert _agy("worker").isolated is True and _agy("worker").not_isolated_because == ""
    assert _agy("worker", verifier=None).isolated is True  # no verifier account configured


def test_a_verifier_account_that_cannot_be_resolved_means_not_isolated(users):
    worker = _agy("worker", verifier="lost-verifier")
    assert worker.isolated is False
    assert worker.not_isolated_because == "verifier account lost-verifier unresolved"


class Isolated(EditWorker):
    isolated = True


def test_the_visible_tests_wrapper_is_never_isolated():
    wrapped = VisibleTestsWorker(Isolated({}), retries=1, pass_fail_only=True)
    assert wrapped.isolated is False  # its probe runs candidate code before verification
    assert "probe runs candidate code" in wrapped.not_isolated_because


# The Issue runner's --clone must be the state's mirror.


@pytest.fixture
def mirror(repo, tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(origin)], check=True)
    path = (tmp_path / "mirror").resolve()
    git(tmp_path, "clone", "-q", "--single-branch", "--branch", "main", str(origin), str(path))
    return path


def _state(tmp_path, mirror_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700, exist_ok=True)
    config: dict[str, Any] = {"verifier_account": False}
    if mirror_path is not None:
        config["mirror"] = str(mirror_path)
    (state / "config.json").write_text(json.dumps(config))
    return state


def _main(state, clone, monkeypatch):
    """main() up to the origin check, which is stubbed to stop: exit 3 with STOP means the mirror
    check passed."""
    stop = RuntimeError("STOP: reached the origin check")
    monkeypatch.setattr(runner, "_verify_origin", lambda c, r: (_ for _ in ()).throw(stop))
    return runner.main(["--state", str(state), "--repo", "o/r", "--clone", str(clone), "run-once"])


def test_a_symlink_alias_of_the_mirror_is_accepted(mirror, tmp_path, monkeypatch, capsys):
    alias = tmp_path / "alias"
    alias.symlink_to(mirror)
    for configured, given in ((alias, mirror), (mirror, alias)):
        assert _main(_state(tmp_path, configured), given, monkeypatch) == 3
        assert '"error": "STOP"' in capsys.readouterr().out


def test_a_case_alias_of_the_mirror_is_accepted(mirror, tmp_path, monkeypatch, capsys):
    alias = mirror.parent / mirror.name.upper()
    if not alias.exists():
        pytest.skip("case-sensitive filesystem: no case alias")
    assert _main(_state(tmp_path, mirror), alias, monkeypatch) == 3
    assert '"error": "STOP"' in capsys.readouterr().out


def test_another_clone_of_the_same_origin_is_refused(mirror, tmp_path, monkeypatch, capsys):
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), str(other))
    assert _main(_state(tmp_path, mirror), other, monkeypatch) == 3
    assert '"error": "CLONE_NOT_STATE_MIRROR"' in capsys.readouterr().out


def test_a_copy_whose_config_diverged_is_refused(mirror, tmp_path, monkeypatch, capsys):
    copy = tmp_path / "copy"
    shutil.copytree(mirror, copy, symlinks=True)
    git(copy, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    assert _main(_state(tmp_path, mirror), copy, monkeypatch) == 3
    assert '"error": "CLONE_NOT_STATE_MIRROR"' in capsys.readouterr().out
    assert Intake(_state(tmp_path, mirror)).clone_problem(copy).startswith("CLONE_NOT_STATE_MIRROR")


@pytest.mark.parametrize("text", ['{"verifier_account": fal', '{"nonsense": 1}'])
def test_a_bad_config_is_exit_3_at_startup(mirror, tmp_path, monkeypatch, capsys, text):
    state = _state(tmp_path, mirror)
    (state / "config.json").write_text(text)  # truncated, or valid JSON with an unknown key
    assert _main(state, mirror, monkeypatch) == 3
    assert '"error": "BAD_CONFIG"' in capsys.readouterr().out


def test_without_a_mirror_there_is_nothing_to_compare(mirror, tmp_path):
    assert Intake(_state(tmp_path, None)).clone_problem(Path(mirror)) == ""


# Follow-ups from the review of eee7a0a.


def test_only_a_missing_marker_means_the_tests_were_not_consumed(tmp_path):
    from cairnwork.verify_candidate import tests_consumed

    intake = Intake(tmp_path / "state")
    intake.prepare()
    oid = "a" * 40
    used = intake.state / "verify-used-tests"
    used.mkdir(exist_ok=True)
    assert tests_consumed(intake, oid) is False and tests_consumed(intake, "not-an-oid") is False
    (used / oid).write_text("")
    assert tests_consumed(intake, oid) is True
    (used / oid).unlink()
    used.chmod(0o000)  # unknown: the marker cannot be looked up
    try:
        assert tests_consumed(intake, oid) is True
    finally:
        used.chmod(0o700)


@pytest.mark.parametrize("err", [errno.EACCES, errno.EIO, errno.ELOOP, errno.ENOTDIR])
def test_any_lookup_error_but_a_missing_marker_means_consumed(tmp_path, monkeypatch, err):
    from cairnwork.verify_candidate import tests_consumed

    intake = Intake(tmp_path / "state")
    intake.prepare()

    def fail(self, *a, **kw):
        if self.name == "a" * 40:
            raise OSError(err, os.strerror(err), str(self))
        return real(self, *a, **kw)

    real = Path.lstat
    monkeypatch.setattr(Path, "lstat", fail)
    monkeypatch.setattr(Path, "stat", fail)  # whichever the lookup uses, below exists()
    assert tests_consumed(intake, "a" * 40) is True


def test_a_state_owned_by_another_uid_is_bad_state(tmp_path, monkeypatch):
    import cairnwork.intake as intake_module
    from cairnwork.intake import IntakeRefused

    intake = Intake(tmp_path / "state")
    intake.prepare()
    intake.check_state()
    monkeypatch.setattr(intake_module.os, "getuid", lambda: os.stat(intake.state).st_uid + 1)
    with pytest.raises(IntakeRefused, match="BAD_STATE: .* not a directory owned by us"):
        intake.check_state()


def test_only_the_typed_busy_error_defers(open_root, stranger):
    _, python = _python_under(open_root, 0o755)

    class Lock(FakeAccount):
        @contextmanager
        def exclusive(self, timeout=None):
            raise VerifierAccountError("RESOURCE_LEASE_TIMEOUT in some other text")
            yield

    account: Any = Lock(str(python))
    with pytest.raises(BaseUncomparable, match="VERIFIER_ACCOUNT_UNUSABLE|lock: "):
        account_check.precheck(account, None)  # the words alone no longer mean busy
