"""#244: two recovery crash windows a TLA+ pilot flagged, verified here. Recovery is unchanged.

1. Parent crash, process sweep unavailable, recovery marks the task FAILED, the operator
   supersedes and resubmits: nothing in Cairnwork stops a process the dead attempt left behind
   from still acting while the new attempt runs. CONFIRMED; pinned below as today's behaviour.
   The only signal is the Decision's ``sweep_unavailable`` (route ``recover`` exits 4, #189).
2. Core recorded a verified verdict, then an exception (here: the verdict readback fails) and a
   crash before the FINISHED journal row: the live run would have said FAILED (INFRA_ERROR), but
   recovery says COMPLETED. Reproduced; INTENDED per recovery.py: a Core verdict verified for the
   journaled candidate is the authority, and only a *journaled* FAILED decision overrides it.
"""

import os
import sqlite3
import subprocess
import sys
import time

import pytest
from conftest import git
from test_pipeline import VERIFY, EditWorker
from test_recovery import GOOD, crash, settle, stores
from test_recovery import project as project  # noqa: F401 - fixture

import cairnwork.recovery as recovery
from cairnwork.confined import ATTEMPT_ENV, SWEEP_UNAVAILABLE
from cairnwork.journal import Journal
from cairnwork.notary import Notary
from cairnwork.ownership import OwnershipLedger
from cairnwork.pipeline import TaskSpec, run_task

pytestmark = pytest.mark.sandbox

# The dead attempt's child: waits until the new attempt's worker is running, then acts.
SURVIVOR = """
import pathlib, sys, time
go, wrote = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
deadline = time.monotonic() + 120
while not go.exists() and time.monotonic() < deadline:
    time.sleep(0.05)
wrote.write_text("old attempt acted while the new one ran")
time.sleep(120)
"""


def _spec(project, key):  # noqa: F811 - the crash driver's task (tests/crash_driver.py)
    base = git(project, "rev-parse", "HEAD")
    return TaskSpec(
        key, "card-k1", "fix add", "add() is off by one", project, base, ("src/calc.py",), VERIFY
    )


class _WaitsForTheSurvivor(EditWorker):
    def __init__(self, go, wrote):
        super().__init__({"src/calc.py": GOOD})
        self.go, self.wrote, self.saw = go, wrote, False

    def run(self, workdir, task):
        super().run(workdir, task)
        self.go.write_text("x")
        deadline = time.monotonic() + 60
        while not self.wrote.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.saw = self.wrote.exists()


def test_a_survivor_of_an_unswept_crash_still_acts_after_supersede_and_resubmit(
    project,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    """#244 window 1 (recovery.py _settle, runlock.py: children do not hold the run lock)."""
    crash(project, tmp_path, "WORKER_STARTED")
    ledger, notary, journal = stores(tmp_path)
    (dead,) = ledger.in_flight()
    old_attempt = journal.entries(dead.task_id)[-1].attempt_id
    go, wrote = tmp_path / "new-attempt-running", tmp_path / "old-attempt-wrote"
    survivor = subprocess.Popen(  # what the dead run's worker started, tagged like AgyWorker's
        [sys.executable, "-c", SURVIVOR, str(go), str(wrote)],
        env={**os.environ, ATTEMPT_ENV: old_attempt},
        start_new_session=True,
    )
    try:
        monkeypatch.setattr(recovery, "sweep_marked", lambda *_a, **_k: SWEEP_UNAVAILABLE)
        (decision,), _ = settle(tmp_path)
        assert (decision.new_status, decision.sweep_unavailable) == ("FAILED", True)
        assert survivor.poll() is None  # recovery could not stop it, and said so
        ledger.set_status(dead.task_id, "SUPERSEDED")
        worker = _WaitsForTheSurvivor(go, wrote)
        result = run_task(  # nothing refuses the takeover
            _spec(project, "k2"),
            ledger=ledger,
            notary=notary,
            journal=journal,
            worker=worker,
            run_root=tmp_path / "runs",
        )
        assert result.status == "COMPLETED" and result.verified, result.reason
        assert worker.saw  # the old attempt's process acted during the new attempt's worker run
        assert survivor.poll() is None  # and is still alive after the new attempt finished
        assert result.attempt_id != old_attempt
    finally:
        survivor.kill()
        survivor.wait()


class _ReadbackFails(Notary):
    """Core records the verdict; reading it back then fails (e.g. a locked or broken disk)."""

    def verdict(self, task_id, attempt_id, *, expected_candidate):
        raise sqlite3.OperationalError("disk I/O error")


class _SimulatedCrash(BaseException):
    """Not an Exception: nothing in the pipeline catches it, like a process death."""


class _CrashBeforeFinished(Journal):
    def record(self, task_id, attempt_id, phase, detail=""):
        if phase == "FINISHED":
            raise _SimulatedCrash(detail)
        super().record(task_id, attempt_id, phase, detail)


def _run_good_fix(project, tmp_path, journal):  # noqa: F811
    return run_task(
        _spec(project, "k1"),
        ledger=OwnershipLedger(tmp_path / "own.sqlite"),
        notary=_ReadbackFails(tmp_path / "notary.sqlite"),
        journal=journal,
        worker=EditWorker({"src/calc.py": GOOD}),
        run_root=tmp_path / "runs",
    )


def test_an_error_after_the_core_verdict_fails_the_live_run(project, tmp_path):  # noqa: F811
    """#244 window 2, no crash: the live run decides FAILED and journals it."""
    result = _run_good_fix(project, tmp_path, Journal(tmp_path / "journal.sqlite"))
    assert result.status == "FAILED" and not result.verified
    assert result.reason.startswith("INFRA_ERROR: OperationalError"), result.reason
    ledger, notary, journal = stores(tmp_path)
    assert ledger.get(task_id=result.task_id).status == "FAILED"
    last = journal.entries(result.task_id)[-1]
    assert (last.phase, last.detail) == ("FINISHED", "FAILED")
    verdict = notary.verdict(
        result.task_id, result.attempt_id, expected_candidate=result.candidate_commit or ""
    )
    assert verdict is not None and verdict.verified  # Core did verify the candidate


def test_a_crash_between_that_error_and_finished_recovers_completed(project, tmp_path):  # noqa: F811
    """#244 window 2 with a crash before FINISHED: recovery trusts the Core verdict."""
    with pytest.raises(_SimulatedCrash, match="FAILED"):  # the run had decided FAILED
        _run_good_fix(project, tmp_path, _CrashBeforeFinished(tmp_path / "journal.sqlite"))
    ledger, notary, journal = stores(tmp_path)
    (task,) = ledger.in_flight()
    assert task.status == "DISPATCHED"
    phases = [e.phase for e in journal.entries(task.task_id)]
    assert phases[-1] == "NOTARIZING" and "FINISHED" not in phases
    decisions, _ = settle(tmp_path)
    assert [(d.new_status, d.reason) for d in decisions] == [
        ("COMPLETED", "CORE_VERIFIED_BEFORE_CRASH")
    ]
    assert ledger.get(task_id=task.task_id).status == "COMPLETED"
