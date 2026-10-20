"""Restart recovery (preserved behavior #21): settle what a crash left behind.

Rules (from Nexus-new's experience, see docs/LESSONS_FROM_NEXUS.md):

- run only when no Cairnwork run is in progress: recovery needs the exclusive
  run lock (runlock.py), which the kernel releases when a run dies;
- never re-run an interrupted attempt. A task becomes COMPLETED only if Core
  already recorded a verdict that is verified for the candidate the journal
  names and the run had not already decided FAILED; every other in-flight
  task becomes FAILED with an INTERRUPTED reason.
  To try again, supersede it and submit a new key (pipeline.py);
- kill any worker the dead run left behind (found by its CAIRNWORK_ATTEMPT
  environment marker) and remove the attempt's clones;
- settle each task on its own: one bad record is reported, not fatal, and
  recovery is idempotent, so it can run again after a crash during recovery.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from cairnwork import runlock
from cairnwork.confined import ATTEMPT_ENV, sweep_marked
from cairnwork.journal import Journal
from cairnwork.notary import Notary
from cairnwork.ownership import OwnershipLedger, OwnershipRecord
from cairnwork.verifier_account import VerifierAccount
from cairnwork.workspace import ATTEMPT_ID, WorkspaceError, remove_attempt_clone


@dataclass(frozen=True)
class Decision:
    task_id: str
    attempt_id: str
    previous_status: str
    new_status: str
    reason: str
    killed_processes: int = 0


def recover(
    *,
    ledger: OwnershipLedger,
    notary: Notary,
    journal: Journal,
    run_root: Path,
    verifier_account: VerifierAccount | None = None,
) -> list[Decision]:
    """Settle every BOUND or DISPATCHED task. Raises RunLockBusy if a run is active."""
    decisions = []
    with runlock.held(ledger.path, exclusive=True):
        if verifier_account is not None:
            with verifier_account.exclusive():
                verifier_account.require_idle()  # a dead run's verifier ran as that account
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
    if not entries and record.status == "DISPATCHED":
        # the journal is written before DISPATCHED, so this is the wrong or a lost journal
        raise RuntimeError("DISPATCHED task has no journal entries; is this the right journal?")
    if not entries:
        ledger.set_status(record.task_id, "FAILED")
        return Decision(record.task_id, "", record.status, "FAILED", "INTERRUPTED_BEFORE_DISPATCH")
    attempt_id = entries[-1].attempt_id
    phases = [e for e in entries if e.attempt_id == attempt_id]
    killed = sweep_marked(ATTEMPT_ENV, attempt_id)  # SWEEP_UNAVAILABLE (-1): not checked
    dispatched = [e.detail for e in phases if e.phase == "DISPATCHED" and e.detail]
    _remove_leftovers(Path(dispatched[-1]) if dispatched else Path(run_root), attempt_id)
    candidates = [e.detail for e in phases if e.phase == "CANDIDATE"]
    finished = [e.detail for e in phases if e.phase == "FINISHED"]
    verified = False
    if candidates and finished[-1:] != ["FAILED"]:  # the run's own FAILED decision stands
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


def _remove_leftovers(run_root: Path, attempt_id: str) -> None:
    """Attempt and verifier clones, a snapshot index, and verifier scratch (run/) dirs.
    Verifier logs (the private dirs themselves) are kept as evidence."""
    if not ATTEMPT_ID.fullmatch(attempt_id):  # the journal is a file: do not trust it for paths
        return
    root = Path(run_root)
    (root / f"{attempt_id}.index").unlink(missing_ok=True)
    (root / f"{attempt_id}.tests.index").unlink(missing_ok=True)
    for private in root.glob(f"{attempt_id}-[vb]-v*.*"):  # verifier and base-test runs
        if private.is_dir() and not private.is_symlink():
            shutil.rmtree(private / "run", ignore_errors=True)
    names = [attempt_id] + sorted(
        p.name for p in root.glob(f"{attempt_id}-[vb]-v*") if p.is_dir() and "." not in p.name
    )
    for name in names:
        if (root / name).is_dir():
            try:
                remove_attempt_clone(root, name)
            except WorkspaceError:
                pass  # a leftover directory is disk space, not a wrong status
