"""Restart recovery (preserved behavior #21): settle what a crash left behind.

Rules (from Nexus-new's experience, see docs/LESSONS_FROM_NEXUS.md):

- run only when no Cairnwork run is in progress: recovery needs the exclusive
  run lock (runlock.py), which the kernel releases when a run dies;
- never re-run an interrupted attempt. A task becomes COMPLETED only if Core
  already recorded a verdict that is verified for the candidate the journal
  names; every other in-flight task becomes FAILED with an INTERRUPTED reason.
  To try again, supersede it and submit a new key (pipeline.py);
- kill any worker the dead run left behind (found by its CAIRNWORK_ATTEMPT
  environment marker) and remove the attempt's clones;
- settle each task on its own: one bad record is reported, not fatal, and
  recovery is idempotent, so it can run again after a crash during recovery.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cairnwork import runlock
from cairnwork.confined import sweep_marked
from cairnwork.journal import Journal
from cairnwork.notary import Notary
from cairnwork.ownership import OwnershipLedger, OwnershipRecord
from cairnwork.workers.agy import ATTEMPT_ENV
from cairnwork.workspace import WorkspaceError, remove_attempt_clone


@dataclass(frozen=True)
class Decision:
    task_id: str
    attempt_id: str
    previous_status: str
    new_status: str
    reason: str
    killed_processes: int = 0


def recover(
    *, ledger: OwnershipLedger, notary: Notary, journal: Journal, run_root: Path
) -> list[Decision]:
    """Settle every BOUND or DISPATCHED task. Raises RunLockBusy if a run is active."""
    decisions = []
    with runlock.held(run_root, exclusive=True):
        for record in ledger.in_flight():
            try:
                decisions.append(_settle(record, ledger, notary, journal, run_root))
            except Exception as exc:  # noqa: BLE001 - one bad record must not stop the others
                decisions.append(
                    Decision(
                        record.task_id,
                        "",
                        record.status,
                        record.status,
                        f"RECOVERY_ERROR: {type(exc).__name__}: {exc}",
                    )
                )
    return decisions


def _settle(record: OwnershipRecord, ledger, notary, journal, run_root) -> Decision:
    entries = journal.entries(record.task_id)
    if not entries:
        ledger.set_status(record.task_id, "FAILED")
        return Decision(record.task_id, "", record.status, "FAILED", "INTERRUPTED_BEFORE_DISPATCH")
    attempt_id = entries[-1].attempt_id
    phases = [e for e in entries if e.attempt_id == attempt_id]
    killed = sweep_marked(ATTEMPT_ENV, attempt_id)
    _remove_clones(run_root, attempt_id)
    candidates = [e.detail for e in phases if e.phase == "CANDIDATE"]
    verified = False
    if candidates:
        verdict = notary.verdict(record.task_id, attempt_id, expected_candidate=candidates[-1])
        verified = bool(verdict and verdict.verified)
    if verified:
        if record.status == "BOUND":
            ledger.set_status(record.task_id, "DISPATCHED")
        ledger.set_status(record.task_id, "COMPLETED")
        return Decision(
            record.task_id,
            attempt_id,
            record.status,
            "COMPLETED",
            "CORE_VERIFIED_BEFORE_CRASH",
            killed,
        )
    ledger.set_status(record.task_id, "FAILED")
    return Decision(
        record.task_id,
        attempt_id,
        record.status,
        "FAILED",
        f"INTERRUPTED_AFTER_{phases[-1].phase}",
        killed,
    )


def _remove_clones(run_root: Path, attempt_id: str) -> None:
    root = Path(run_root)
    names = [attempt_id] + sorted(
        p.name for p in root.glob(f"{attempt_id}-v-v*") if p.is_dir() and "." not in p.name
    )
    for name in names:
        if (root / name).is_dir():
            try:
                remove_attempt_clone(root, name)
            except WorkspaceError:
                pass  # a leftover directory is disk space, not a wrong status
