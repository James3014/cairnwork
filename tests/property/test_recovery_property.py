"""Property test for restart recovery (preserved behavior #21).

recover() settles every in-flight (BOUND/DISPATCHED) task after a crash. The invariant that
must never break: a task becomes COMPLETED only when Core already recorded a verdict that is
verified for the candidate the journal names, the run did not already decide FAILED, and a
candidate was actually produced; everything else fails closed, and a DISPATCHED task with no
journal is a recovery error, never a silent completion. This drives recover() over random
crash states (seeded ledger + journal + a stub notary) and checks the decision against an
independent hand-written oracle, then checks recovery is idempotent.

The notary is stubbed so this exercises recovery's decision logic, not Core verification
(which notary's own tests cover): recover() only reads verdict.verified.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from hypothesis import given, settings
from hypothesis import strategies as st

from cairnwork.journal import Journal
from cairnwork.ownership import OwnershipLedger
from cairnwork.recovery import recover

ATTEMPT = "a1"
CANDIDATE_COMMIT = "0" * 39 + "1"


class _StubNotary:
    """Stands in for Core: recover() only uses verdict.verified. verified=False returns no
    recorded verdict (None), like a crash before Core answered."""

    def __init__(self, verified: bool) -> None:
        self._verified = verified

    def verdict(self, task_id, attempt_id, *, expected_candidate):
        return SimpleNamespace(verified=True) if self._verified else None


def _expected_status(status, dispatched, candidate, finished, verified):
    """The outcome recover() must reach, written independently of recovery.py."""
    has_entries = dispatched or candidate or finished is not None
    if not has_entries:
        return status if status == "DISPATCHED" else "FAILED"  # DISPATCHED+no journal => error
    would_check = candidate and finished != "FAILED"
    return "COMPLETED" if (would_check and verified) else "FAILED"


@settings(max_examples=250, deadline=None)
@given(
    status=st.sampled_from(["BOUND", "DISPATCHED"]),
    dispatched=st.booleans(),
    candidate=st.booleans(),
    finished=st.sampled_from([None, "COMPLETED", "FAILED"]),
    verified=st.booleans(),
)
def test_recovery_only_completes_on_a_verified_verdict(
    status, dispatched, candidate, finished, verified
):
    work = Path(tempfile.mkdtemp(prefix="recovery-prop-"))
    try:
        ledger = OwnershipLedger(work / "own.sqlite")
        task_id = ledger.bind("k", {"x": 1}, "cairnwork", "card").record.task_id
        if status == "DISPATCHED":
            ledger.set_status(task_id, "DISPATCHED")

        journal = Journal(work / "journal.sqlite")
        if dispatched:
            journal.record(task_id, ATTEMPT, "DISPATCHED", str(work / "runs"))
        if candidate:
            journal.record(task_id, ATTEMPT, "CANDIDATE", CANDIDATE_COMMIT)
        if finished is not None:
            journal.record(task_id, ATTEMPT, "FINISHED", finished)
        (work / "runs").mkdir(exist_ok=True)

        decisions = recover(
            ledger=ledger, notary=_StubNotary(verified), journal=journal, run_root=work / "runs"
        )

        expected = _expected_status(status, dispatched, candidate, finished, verified)
        mine = next(d for d in decisions if d.task_id == task_id)
        if expected == "DISPATCHED":  # the recovery-error path leaves the status unchanged
            assert mine.new_status == "DISPATCHED" and mine.reason.startswith("RECOVERY_ERROR")
            assert ledger.get(task_id=task_id).status == "DISPATCHED"
        else:
            assert mine.new_status == expected, mine
            assert ledger.get(task_id=task_id).status == expected

        # idempotent: a second recovery settles nothing further (the task is no longer in-flight,
        # unless it is the recovery-error case that stayed DISPATCHED)
        again = recover(
            ledger=ledger, notary=_StubNotary(verified), journal=journal, run_root=work / "runs"
        )
        if expected != "DISPATCHED":
            assert all(d.task_id != task_id for d in again)
            assert ledger.get(task_id=task_id).status == expected
    finally:
        import shutil

        shutil.rmtree(work, ignore_errors=True)
