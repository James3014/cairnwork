"""#234 PR-C: the account hint and precheck, worker isolation by uid, and the Issue runner's
mirror invariant. The verify and worker paths are covered in test_uncomparable_paths.py."""

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
from cairnwork.verifier_account import VerifierAccountError
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
            raise VerifierAccountError(f"RESOURCE_LEASE_TIMEOUT: busy for {timeout}s")
        yield

    def available(self, python=None):
        return self.usable


def test_precheck_takes_its_verdict_from_available_and_its_reason_from_the_walk(
    open_root, stranger
):
    home, python = _python_under(open_root, 0o750)
    account_check.precheck(None, None)  # no account: nothing to check
    usable: Any = FakeAccount(str(python), usable=True)
    account_check.precheck(usable, None)  # mode bits say no, available() says yes: it decides
    broken: Any = FakeAccount(str(python))
    with pytest.raises(BaseUncomparable) as caught:
        account_check.precheck(broken, None)
    assert caught.value.comparison.code == "VERIFIER_ACCOUNT_UNUSABLE"
    assert f"{home} 0750" in caught.value.comparison.detail


def test_a_busy_account_is_never_waited_for_and_only_the_walk_may_refuse(open_root, stranger):
    home, python = _python_under(open_root, 0o750)
    busy: Any = FakeAccount(str(python), busy=True)
    with pytest.raises(BaseUncomparable, match=f"busy for 0.0s; {home} 0750"):
        account_check.precheck(busy, None)  # one try (timeout 0), then the mode bits say no
    home.chmod(0o755)
    account_check.precheck(busy, None)  # nothing blocks: the run's own available() decides


def test_an_account_without_a_lock_file_is_unusable(tmp_path):
    from cairnwork.verifier_account import VerifierAccount

    account = VerifierAccount(root=tmp_path / "missing")
    with pytest.raises(BaseUncomparable, match="lock: .*No such file"):
        account_check.precheck(account, sys.executable)


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


class Isolated(EditWorker):
    isolated = True


def test_the_visible_tests_wrapper_passes_isolation_through():
    assert VisibleTestsWorker(Isolated({}), pass_fail_only=True).isolated is True
    plain = VisibleTestsWorker(EditWorker({}), pass_fail_only=True)
    assert plain.isolated is False and plain.not_isolated_because == ""


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


def test_without_a_mirror_there_is_nothing_to_compare(mirror, tmp_path):
    assert Intake(_state(tmp_path, None)).clone_problem(Path(mirror)) == ""
