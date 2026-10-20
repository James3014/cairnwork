"""Intake without starting real runners: the lock hand-over, cancel, and re-checks."""

import os

import pytest
from conftest import git

import cairnwork.intake as intake_module
from cairnwork.intake import Intake, IntakeRefused
from cairnwork.pipeline import TaskRejected, run_bound_task
from cairnwork.recovery import recover
from cairnwork.runlock import RunLockBusy


class FakePopen:
    """Keeps a duplicate of every passed fd, as a started child would."""

    held: list[int] = []

    def __init__(self, argv, pass_fds=(), **_kwargs):
        self.argv = argv
        FakePopen.held.extend(os.dup(fd) for fd in pass_fds)


@pytest.fixture
def intake(tmp_path, monkeypatch):
    monkeypatch.setattr(intake_module, "_spawn", FakePopen)
    FakePopen.held = []
    made = Intake(tmp_path / "state")
    made.set_accepting(True)
    yield made
    for fd in FakePopen.held:
        os.close(fd)


def payload(repo, key="gw-1"):
    return {
        "idempotency_key": key,
        "card_hash": "card-" + key,
        "title": "fix",
        "instructions": "fix it",
        "repo": str(repo),
        "base_commit": git(repo, "rev-parse", "HEAD"),
        "allowed_files": ["src/a.py"],
        "verifiers": [["pytest", ["python3", "-m", "pytest", "tests/test_a.py"]]],
    }


def settle(intake):
    return recover(
        ledger=intake.ledger,
        notary=intake.notary,
        journal=intake.journal,
        run_root=intake.run_root,
    )


def test_the_runner_inherits_the_run_lock_so_recovery_waits(intake, repo):
    out = intake.submit(payload(repo))
    assert out["status"] == "BOUND" and len(FakePopen.held) == 1
    with pytest.raises(RunLockBusy):
        settle(intake)  # the "runner" holds the lock: the task cannot be settled yet
    os.close(FakePopen.held.pop())  # the runner died
    assert [d.new_status for d in settle(intake)] == ["FAILED"]


def test_a_task_not_started_can_be_cancelled_and_is_then_never_run(intake, repo):
    out = intake.submit(payload(repo))
    assert intake.cancel(out["task_id"])["status"] == "CANCELLED"
    with pytest.raises(IntakeRefused, match="NOT_CANCELLABLE"):
        intake.cancel(out["task_id"])
    result = run_bound_task(
        intake.load_request(out["task_id"]),
        out["task_id"],
        ledger=intake.ledger,
        notary=intake.notary,
        journal=intake.journal,
        worker=None,
        run_root=intake.run_root,
    )
    assert result.reason == "NOT_BOUND" and result.status == "CANCELLED"


def test_a_runner_refuses_a_request_that_is_not_the_bound_one(intake, repo):
    out = intake.submit(payload(repo))
    other = intake_module.task_from_payload({**payload(repo), "title": "changed"})
    with pytest.raises(TaskRejected, match="REQUEST_MISMATCH"):
        run_bound_task(
            other,
            out["task_id"],
            ledger=intake.ledger,
            notary=intake.notary,
            journal=intake.journal,
            worker=None,
            run_root=intake.run_root,
        )


def test_a_failed_start_leaves_the_task_failed_not_bound(intake, repo, monkeypatch):
    def broken(*_args, **_kwargs):
        raise OSError("cannot start")

    monkeypatch.setattr(intake_module, "_spawn", broken)
    with pytest.raises(OSError):
        intake.submit(payload(repo))
    assert intake.status(key="gw-1")["status"] == "FAILED"


def test_bad_config_is_refused(intake):
    (intake.state / "config.json").write_text('{"modle": "x"}')
    with pytest.raises(IntakeRefused, match="BAD_CONFIG"):
        intake.config()
