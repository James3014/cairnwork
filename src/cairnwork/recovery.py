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

import json
import os
import shutil
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from cairnwork import runlock
from cairnwork.confined import ATTEMPT_ENV, sweep_marked
from cairnwork.journal import Journal
from cairnwork.notary import Notary
from cairnwork.ownership import OWNER, OwnershipLedger, OwnershipRecord
from cairnwork.reasons import _names
from cairnwork.verifier_account import VerifierAccount, VerifierAccountError
from cairnwork.workspace import ATTEMPT_ID, WorkspaceError, remove_attempt_clone


@dataclass(frozen=True)
class Decision:
    task_id: str
    attempt_id: str
    previous_status: str
    new_status: str
    reason: str
    killed_processes: int = 0
    detail: str = ""  # e.g. "LEFTOVER_RUN_DIRS: 2: a1-q-v0, a1-r-v0.x"; "" when nothing remains
    leftover_run_dirs: int = 0  # how many run directories the cleanup could not remove


def recover(
    *,
    ledger: OwnershipLedger,
    notary: Notary,
    journal: Journal,
    run_root: Path,
    verifier_account: VerifierAccount | None = None,
    leftovers_log: Path | None = None,
) -> list[Decision]:
    """Settle every BOUND or DISPATCHED task. Raises RunLockBusy if a run is active.
    Directories that could not be removed go in the Decision and, when ``leftovers_log`` is
    given, in that append-only JSON-lines file; never in the journal (a journal row would
    change what a later recovery decides)."""
    decisions = []
    # Hold the account's lock across the whole settle loop: a dead run's verifier ran as that
    # account, and settling removes its leftover run directories (owned by the account) with
    # account.remove, so no other run may touch the account meanwhile.
    account_lock = verifier_account.exclusive() if verifier_account is not None else nullcontext()
    with runlock.held(ledger.path, exclusive=True), account_lock:
        if verifier_account is not None:
            verifier_account.require_idle()
        for record in ledger.in_flight(include_foreign=True):
            if record.owner != OWNER:  # not ours: report it, never settle it
                decisions.append(
                    Decision(record.task_id, "", record.status, record.status, "FOREIGN_OWNER")
                )
                continue
            try:
                decisions.append(
                    _settle(
                        record, ledger, notary, journal, run_root, verifier_account, leftovers_log
                    )
                )
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


def _settle(
    record: OwnershipRecord, ledger, notary, journal, run_root, account=None, leftovers_log=None
) -> Decision:
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
    roots = [Path(run_root)]  # the state's run root, and the root this attempt was dispatched under
    if dispatched and Path(dispatched[-1]) != roots[0]:
        roots.append(Path(dispatched[-1]))
    left = []
    for owner_root in roots:
        _remove_leftovers(owner_root, attempt_id)
    if account is not None:  # the verifier ran under the account's run root; the owner cannot rm it
        _remove_account_leftovers(account, attempt_id)
    for owner_root in roots:
        left += _leftover_names(owner_root, attempt_id)
    if account is not None:
        left += _leftover_names(account.run_root, attempt_id)
    detail = f"LEFTOVER_RUN_DIRS: {_names(left)}" if left else ""
    if left and leftovers_log is not None:
        try:
            _log_leftovers(leftovers_log, record.task_id, attempt_id, left)
        except OSError:
            detail += " (leftover log write failed)"  # the Decision still carries the signal
    candidates = [e.detail for e in phases if e.phase == "CANDIDATE"]
    finished = [e.detail for e in phases if e.phase == "FINISHED" and e.detail in _RESULTS]
    verified = False
    if candidates and finished[-1:] != ["FAILED"]:  # the run's own FAILED decision stands
        verdict = notary.verdict(record.task_id, attempt_id, expected_candidate=candidates[-1])
        verified = bool(verdict and verdict.verified)
    if verified:
        if record.status == "BOUND":
            ledger.set_status(record.task_id, "DISPATCHED")
        new, reason = "COMPLETED", "CORE_VERIFIED_BEFORE_CRASH"
    else:
        new, reason = "FAILED", f"INTERRUPTED_AFTER_{phases[-1].phase}"
    ledger.set_status(record.task_id, new)
    return Decision(
        record.task_id, attempt_id, record.status, new, reason, killed, detail, len(left)
    )


def _log_leftovers(path: Path, task_id: str, attempt_id: str, left: list[str]) -> None:
    """Append one JSON line (0600, never following a symlink): time, task, attempt, count, names."""
    row = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "task_id": task_id,
           "attempt_id": attempt_id, "count": len(left), "names": _names(left)}  # fmt: skip
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "a") as out:
        os.fchmod(out.fileno(), 0o600)
        out.write(json.dumps(row, sort_keys=True) + "\n")


_RESULTS = ("COMPLETED", "FAILED")  # the run's own FINISHED details (pipeline.py)
_RUN_TAGS = "vbpqr"  # verify, base-test, and the P2P probe / candidate run / re-run (p2p.py)


def _run_dirs(root: Path, attempt_id: str) -> tuple[list[Path], list[Path], list[Path]]:
    """The attempt's run directories under ``root``: verifier checkouts (``.work``), private
    run dirs (``.priv``...; a verify log stays as evidence, only its run/ goes) and clones.
    One walk for the owner side and the verifier-account side, so the two cannot drift."""
    pattern = f"{attempt_id}-[{_RUN_TAGS}]-v*"

    # symlinks are listed too (dangling or not): they are unlinked, never followed (_remove_inside)
    def real(p: Path) -> bool:
        return p.is_symlink() or p.is_dir()

    works = [w for w in root.glob(f"{pattern}.work") if real(w)]
    privates = []
    for private in root.glob(f"{pattern}.*"):  # verifier, base-test and P2P runs
        if private.suffix == ".work" or not real(private):
            continue
        # a base-test or P2P run is not Core evidence and would show the tests: all of it goes
        keep_log = f"{attempt_id}-v-" in private.name and not private.is_symlink()
        privates.append(private / "run" if keep_log else private)
    clones = sorted(p for p in root.glob(pattern) if real(p) and "." not in p.name)
    return works, privates, clones


def _remove_leftovers(run_root: Path, attempt_id: str) -> None:
    """Attempt and verifier clones, a snapshot index, and verifier scratch (run/) dirs.
    Verifier logs (the private dirs themselves) are kept as evidence."""
    if not ATTEMPT_ID.fullmatch(attempt_id):  # the journal is a file: do not trust it for paths
        return
    root = Path(run_root)
    (root / f"{attempt_id}.index").unlink(missing_ok=True)
    (root / f"{attempt_id}.tests.index").unlink(missing_ok=True)
    works, privates, clones = _run_dirs(root, attempt_id)
    for work in works:  # a verifier checkout's own dir
        if not work.is_symlink():
            try:
                remove_attempt_clone(work, work.name.removesuffix(".work"))
            except WorkspaceError:
                pass
        _remove_inside(root, work)
    for private in privates:
        _remove_inside(root, private)
    for name in [attempt_id] + [p.name for p in clones]:
        path = root / name
        if path.is_symlink():
            _unlink(path)
        elif path.is_dir():
            try:
                remove_attempt_clone(root, name)
            except WorkspaceError:
                pass  # a leftover directory is disk space, not a wrong status


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass  # the re-scan reports what is still there


def _remove_inside(root: Path, target: Path) -> None:
    """Delete ``target`` only if it is strictly inside ``root``. A symlink is unlinked itself,
    never followed: a name the dead run (or its account) swapped must not aim rmtree elsewhere."""
    if target.is_symlink():
        _unlink(target)
        return
    real = Path(os.path.realpath(target))
    if Path(os.path.realpath(root)) in real.parents:
        shutil.rmtree(target, ignore_errors=True)


def _remove_account_leftovers(account: VerifierAccount, attempt_id: str) -> None:
    """Remove the attempt's verifier run directories under the account's OWN run root.

    When a verifier account is used the verifier clones and scratch live under
    ``account.run_root`` and are owned by the account, so only ``account.remove`` (chmod + rm
    as that account) can delete them — the owner-side ``_remove_leftovers`` cannot. This mirrors
    normal cleanup (verifiers.py removes the same dirs with ``account.remove``) and keeps the
    verify run's log directory as evidence, as ``_remove_leftovers`` does."""
    if not ATTEMPT_ID.fullmatch(attempt_id):  # the journal is a file: do not trust it for paths
        return
    root = account.run_root
    if not root.is_dir():
        return
    works, privates, clones = _run_dirs(root, attempt_id)
    targets = works + privates + clones
    for target in targets:
        try:
            account.remove(target)
        except VerifierAccountError:
            pass  # the re-scan after cleanup reports what is still there (_leftover_names)
        # the verifier checkout and its work dir are the OWNER's (verifiers.py makes them), so
        # the account's rm cannot remove them: the owner finishes what the account could not
        _remove_inside(root, target)


def _leftover_names(root: Path, attempt_id: str) -> list[str]:
    """Names (only) of the attempt's directories still under ``root`` after cleanup: anything
    that can hold laid-over hidden tests. A verify run's private dir is evidence and does not
    count, its ``run/`` scratch does. Re-scans from disk; it trusts nothing the removers said."""
    if not ATTEMPT_ID.fullmatch(attempt_id) or not Path(root).is_dir():
        return []
    root = Path(root)
    works, privates, clones = _run_dirs(root, attempt_id)
    own = [root / attempt_id] if os.path.lexists(root / attempt_id) else []
    names = {
        p.name if p.name != "run" else p.parent.name + "/run"
        for p in works + privates + clones + own
        if os.path.lexists(p)  # a dangling symlink is still a leftover
    }
    return sorted(names)
