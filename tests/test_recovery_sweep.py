"""Recovery reports an unavailable process sweep instead of claiming a clean recovery (#189):
``sweep_marked`` returning SWEEP_UNAVAILABLE means "may still be running", never "none"."""

import json

import pytest
from test_recovery import GOOD, crash, settle, stores
from test_recovery import project as project  # noqa: F401 - fixture
from test_recovery_leftovers import _routable, _stuck_dir

import cairnwork.recovery as recovery
from cairnwork import route
from cairnwork.confined import SWEEP_UNAVAILABLE

NOTE = "SWEEP_UNAVAILABLE: processes of this attempt were not checked and may still run"


@pytest.fixture
def unavailable_sweep(monkeypatch):
    monkeypatch.setattr(recovery, "sweep_marked", lambda *_a, **_k: SWEEP_UNAVAILABLE)


@pytest.fixture
def working_sweep(monkeypatch):
    monkeypatch.setattr(recovery, "sweep_marked", lambda *_a, **_k: 0)


def test_an_unavailable_sweep_is_reported_and_the_task_still_fails(  # noqa: F811
    project, tmp_path, unavailable_sweep
):
    crash(project, tmp_path, "CANDIDATE")
    (decision,), ledger = settle(tmp_path)
    assert (decision.new_status, decision.reason) == ("FAILED", "INTERRUPTED_AFTER_CANDIDATE")
    assert decision.sweep_unavailable is True
    assert decision.killed_processes == SWEEP_UNAVAILABLE
    assert decision.detail == NOTE
    assert decision.leftover_run_dirs == 0
    assert ledger.get(task_id=decision.task_id).status == "FAILED"


def test_a_working_sweep_is_not_flagged(project, tmp_path, working_sweep):  # noqa: F811
    crash(project, tmp_path, "CANDIDATE")
    (decision,), _ = settle(tmp_path)
    assert decision.sweep_unavailable is False
    assert decision.detail == ""
    assert decision.killed_processes == 0


@pytest.mark.sandbox
def test_core_verified_before_the_crash_still_completes_but_is_flagged(  # noqa: F811
    project, tmp_path, unavailable_sweep
):
    crash(project, tmp_path, "FINISHED", GOOD)
    (decision,), ledger = settle(tmp_path)
    assert (decision.new_status, decision.reason) == ("COMPLETED", "CORE_VERIFIED_BEFORE_CRASH")
    assert decision.sweep_unavailable is True
    assert decision.detail == NOTE
    assert ledger.get(task_id=decision.task_id).status == "COMPLETED"


def test_unavailable_sweep_and_leftovers_are_both_in_the_detail(  # noqa: F811
    project, tmp_path, unavailable_sweep
):
    crash(project, tmp_path, "CANDIDATE")
    ledger, _, journal = stores(tmp_path)
    task = ledger.in_flight()[0].task_id
    attempt = journal.entries(task)[-1].attempt_id
    stuck = _stuck_dir(tmp_path, attempt, "q")
    try:
        (decision,), _ = settle(tmp_path)
    finally:
        (tmp_path / "runs").chmod(0o700)
    assert decision.detail == f"{NOTE}; LEFTOVER_RUN_DIRS: 2: {attempt}, {stuck.name}"
    assert decision.leftover_run_dirs == 2
    assert decision.sweep_unavailable is True


def test_route_recover_exits_4_when_the_sweep_was_unavailable(  # noqa: F811
    project, tmp_path, unavailable_sweep, capsys
):
    _routable(project, tmp_path)
    code = route.main(["--state", str(tmp_path), "recover"])
    out = json.loads(capsys.readouterr().out)
    assert code == 4
    assert out["sweep_unavailable"] is True and out["leftover_run_dirs"] == 0
    assert out["decisions"][0]["sweep_unavailable"] is True
    assert out["decisions"][0]["killed_processes"] == SWEEP_UNAVAILABLE


def test_route_recover_exits_0_when_the_sweep_worked(  # noqa: F811
    project, tmp_path, working_sweep, capsys
):
    _routable(project, tmp_path)
    code = route.main(["--state", str(tmp_path), "recover"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["sweep_unavailable"] is False and out["leftover_run_dirs"] == 0
