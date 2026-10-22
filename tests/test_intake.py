"""Intake without starting real runners: the lock hand-over, cancel, and re-checks."""

import json
import os

import pytest
from conftest import git

import cairnwork.intake as intake_module
from cairnwork.intake import DEFAULT_CONFIG, Intake, IntakeRefused
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


def test_cancel_refuses_a_task_another_owner_holds_and_leaves_it_bound(intake):
    task = intake.ledger.bind("k", {"a": 1}, "nexus-new", "c" * 8).record.task_id
    with pytest.raises(IntakeRefused, match="OWNER_MISMATCH"):
        intake.cancel(task)
    assert intake.status(task_id=task)["status"] == "BOUND"


def test_in_flight_ignores_a_task_another_owner_holds(intake):
    intake.ledger.bind("k", {"a": 1}, "nexus-new", "c" * 8)
    assert intake.in_flight()["count"] == 0


def test_cancel_loses_to_a_runner_that_dispatched_first(intake, repo):
    out = intake.submit(payload(repo))
    intake.ledger.set_status(out["task_id"], "DISPATCHED")  # the runner got there first
    with pytest.raises(IntakeRefused, match="NOT_CANCELLABLE"):
        intake.cancel(out["task_id"])
    assert intake.status(task_id=out["task_id"])["status"] == "DISPATCHED"


def test_drain_waits_for_a_submit_that_already_checked_the_flag(intake, repo, monkeypatch):
    import threading

    import cairnwork.runlock as runlock

    entered, release = threading.Event(), threading.Event()
    real = runlock.shared_fd

    def slow(path):
        entered.set()
        release.wait(10)
        return real(path)

    monkeypatch.setattr(intake_module.runlock, "shared_fd", slow)
    submit = threading.Thread(target=intake.submit, args=(payload(repo),))
    submit.start()
    entered.wait(10)  # the submit passed the accepting check
    drain = threading.Thread(target=intake.set_accepting, args=(False,))
    drain.start()
    drain.join(0.5)
    assert drain.is_alive()  # drain cannot finish while that submit is binding
    release.set()
    submit.join(10)
    drain.join(10)
    assert not intake.accepting() and intake.in_flight()["count"] == 1


def test_the_same_key_for_another_repo_is_refused(intake, repo, tmp_path):
    from cairnwork.ownership import KeyReusedWithDifferentRequest

    intake.submit(payload(repo))
    with pytest.raises(KeyReusedWithDifferentRequest):
        intake.submit({**payload(repo), "repo": str(tmp_path / "elsewhere")})


def test_a_state_dir_others_can_use_is_refused(intake, repo):
    intake.state.chmod(0o755)
    with pytest.raises(IntakeRefused, match="BAD_STATE"):
        intake.submit(payload(repo))


def test_in_flight_of_a_missing_state_dir_is_an_error(tmp_path):
    with pytest.raises(IntakeRefused, match="BAD_STATE"):
        Intake(tmp_path / "missing").in_flight()


def test_recover_that_cannot_settle_a_task_does_not_exit_0(intake, repo, capsys):
    from cairnwork.route import main

    (intake.state / "config.json").write_text('{"verifier_account": false}')  # Linux CI
    out = intake.submit(payload(repo))
    intake.ledger.set_status(out["task_id"], "DISPATCHED")  # dispatched, but no journal
    os.close(FakePopen.held.pop())
    assert main(["--state", str(intake.state), "recover"]) == 1
    assert "RECOVERY_ERROR" in capsys.readouterr().out


def test_a_bad_config_refuses_the_submit_before_binding(intake, repo):
    (intake.state / "config.json").write_text('{"modle": "x"}')
    with pytest.raises(IntakeRefused, match="BAD_CONFIG"):
        intake.submit(payload(repo))
    assert intake.in_flight()["count"] == 0 and FakePopen.held == []


@pytest.mark.parametrize(
    "config",
    [
        {"agy_dispatch": "relative/dispatch"},
        {"agy_dispatch": 5},
        {"agy_dispatch_pool_wait_seconds": 0},
        {"agy_dispatch_provider_stall_seconds": "900"},
        {"agy_dispatch_stream_no_progress_seconds": True},
        {"agy_dispatch_pool_wait_seconds": 1.5},
        {"agy_dispatch_pool_wait_seconds": 86401},
        {"agy_dispatch_other": 1},
        {"model": "a\nb"},
        {"model": ""},
        {"model": 3},
    ],
)
def test_bad_agy_dispatch_settings_are_refused(intake, config):
    (intake.state / "config.json").write_text(json.dumps(config))
    with pytest.raises(IntakeRefused, match="BAD_CONFIG"):
        intake.config()


def test_p2p_defaults_and_valid_overrides(intake):
    assert intake.config()["p2p_selection"] == "whole-suite"
    assert intake.config()["p2p_time_budget_seconds"] == 300
    (intake.state / "config.json").write_text(
        json.dumps({"p2p_selection": "off", "p2p_time_budget_seconds": 120})
    )
    assert intake.config()["p2p_selection"] == "off"
    assert intake.config()["p2p_time_budget_seconds"] == 120


@pytest.mark.parametrize(
    "config",
    [{"p2p_selection": "bogus"}, {"p2p_selection": None}, {"p2p_time_budget_seconds": 0},
     {"p2p_time_budget_seconds": "300"}, {"p2p_time_budget_seconds": 86401}],
)  # fmt: skip
def test_bad_p2p_settings_are_refused(intake, config):
    (intake.state / "config.json").write_text(json.dumps(config))
    with pytest.raises(IntakeRefused, match="BAD_CONFIG"):
        intake.config()


def test_agy_dispatch_defaults_to_off_and_accepts_valid_settings(intake):
    assert intake.config()["agy_dispatch"] is None
    assert intake.config()["agy_dispatch_pool_wait_seconds"] == 600
    (intake.state / "config.json").write_text(
        json.dumps({"agy_dispatch": "/opt/pool/dispatch", "agy_dispatch_pool_wait_seconds": 30})
    )
    config = intake.config()
    assert config["agy_dispatch"] == "/opt/pool/dispatch"
    assert config["agy_dispatch_pool_wait_seconds"] == 30
    assert config["agy_dispatch_provider_stall_seconds"] == 900


def test_route_hands_the_dispatcher_to_the_worker(monkeypatch):
    from types import SimpleNamespace

    import cairnwork.route as route

    made = []
    monkeypatch.setattr(route, "AgyWorker", lambda **kw: made.append(kw) or "worker")
    monkeypatch.setattr(route, "run_bound_task", lambda *a, **kw: SimpleNamespace(logs=[]))

    def run(config):
        intake = SimpleNamespace(
            config=lambda: {**DEFAULT_CONFIG, **config},
            load_request=lambda _t: None,
            ledger=None,
            notary=None,
            journal=None,
            run_root=None,
            verifier_account=lambda: None,
        )
        route._run(intake, "t1")  # type: ignore[arg-type]
        return made[-1]

    assert run({})["dispatch"] is None
    got = run(
        {
            "agy_dispatch": "/opt/pool/d",
            "agy_dispatch_pool_wait_seconds": 1,
            "agy_dispatch_provider_stall_seconds": 2,
            "agy_dispatch_stream_no_progress_seconds": 3,
        }
    )["dispatch"]
    assert (got.path, got.pool_wait_seconds, got.provider_stall_seconds) == ("/opt/pool/d", 1, 2)
    assert got.stream_no_progress_seconds == 3
