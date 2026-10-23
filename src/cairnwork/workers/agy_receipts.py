"""The dispatcher's worker receipts: proof that a run ran as the worker account (Issue #183).

With ``--run-as-user`` the dispatcher (Nexus-new #2888) writes, for every attempt
that prepared a worker run, three lines on stderr with the status-line prefix:

    WORKER_LAUNCH   {attempt, operation_id, run_as_user, run_root, run_root_id, ...}
    WORKER_SWEEP    {attempt, result clean|not_started|unclean|error,
                     wrapper CLEAN|UNCLEAN|REFUSED|SWEEP_ERROR, left [pids]}
    WORKER_CLEANUP  {attempt, run_root_removed, worker_failure "KIND:reason" or null}

and its final status line stays last. Anything the agy child printed is relayed
with a ``[provider]`` prefix, so StatusReader never keeps it. Nothing here is
guessed from the process list.

A run counts as isolated only when every attempt's receipts are present once, the
launch names the configured account and a run root under WORKER_RUNS, the sweep
says ``clean`` / ``CLEAN`` with nothing left, the run root was removed and no worker
failure is named; and, for a run that exits 0, the last line is a ``completed``
status whose attempt has a launch receipt. Anything else fails the attempt
(fail closed). On every exit path a post-hoc wrapper ``--sweep`` is run for a launched
run whose receipts do not prove it swept clean and removed, whose run root still exists
and is still the directory (dev, ino) its launch receipt named.

Each run with ``run_as_user`` leaves ``<attempt>.agy.worker.json`` (owner-only) next
to the agy log: the receipts as read, any post-hoc sweep, and the problems found.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from cairnwork.workers import agy_dispatch

LAUNCH, SWEEP, CLEANUP = "WORKER_LAUNCH", "WORKER_SWEEP", "WORKER_CLEANUP"
EVENTS = (LAUNCH, SWEEP, CLEANUP)
SHOWN = 200  # characters of a dispatcher-written value quoted in a message


@dataclass
class Attempt:
    number: object
    receipts: dict = field(default_factory=dict)  # event -> the first receipt of it
    duplicate: bool = False


@dataclass
class Receipts:
    run_as_user: str
    attempts: dict = field(default_factory=dict)  # attempt (as JSON) -> Attempt, in order
    final: dict | None = None  # the last line, when it is a status line and not a receipt
    known: bool = True  # every dispatcher line was read and kept
    post_hoc: dict = field(default_factory=dict)  # run root -> wrapper sweep after a kill


def parse(lines: list[str], run_as_user: str, *, known: bool = True) -> Receipts:
    """Group the WORKER_* receipts by attempt and find the final status line."""
    out = Receipts(run_as_user, known=known)
    metas = [agy_dispatch.read_status(line) for line in lines]
    for meta in metas:
        event = meta.get("event")
        if event not in EVENTS:
            continue
        key = json.dumps(meta.get("attempt"), sort_keys=True)
        attempt = out.attempts.setdefault(key, Attempt(meta.get("attempt")))
        attempt.duplicate |= event in attempt.receipts
        attempt.receipts.setdefault(event, meta)
    last = metas[-1] if metas else {}
    out.final = last if "status" in last and "event" not in last else None
    return out


def is_run_root(path: object) -> bool:
    pattern = re.escape(agy_dispatch.WORKER_RUNS) + r"/[0-9a-f]{32}"
    return isinstance(path, str) and re.fullmatch(pattern, path) is not None


def _shown(value: object) -> str:
    return repr(str(value)[:SHOWN])


def launch_problem(attempt: Attempt, user: str) -> str | None:
    n = attempt.number
    if attempt.duplicate:
        return f"WORKER_RECEIPT_DUPLICATE: attempt {n} has a receipt twice"
    launch = attempt.receipts.get(LAUNCH)
    if launch is None:
        return f"WORKER_RECEIPT_MISSING: attempt {n} has no WORKER_LAUNCH receipt"
    if launch.get("run_as_user") != user:
        ran_as = _shown(launch.get("run_as_user"))
        return f"WORKER_RECEIPT_MISMATCH: attempt {n} ran as {ran_as}, not {user!r}"
    if not is_run_root(launch.get("run_root")):
        root = _shown(launch.get("run_root"))
        return f"WORKER_RECEIPT_MISMATCH: attempt {n} run root {root} is not a worker run"
    return None


def worker_failure(attempt: Attempt) -> str | None:
    """The cleanup receipt's ``KIND:reason`` (only those two segments), or None."""
    failure = (attempt.receipts.get(CLEANUP) or {}).get("worker_failure")
    return ":".join(str(failure).split(":", 2)[:2])[:SHOWN] if failure else None


def sweep_clean(attempt: Attempt) -> bool:
    sweep = attempt.receipts.get(SWEEP) or {}
    return (
        sweep.get("result") == "clean"
        and sweep.get("wrapper") == "CLEAN"
        and sweep.get("left") == []
    )


def attempt_problem(attempt: Attempt, user: str) -> str | None:
    """Why this attempt's receipts do not show an isolated run swept clean, or None."""
    if problem := launch_problem(attempt, user):
        return problem
    n = attempt.number
    if SWEEP not in attempt.receipts or CLEANUP not in attempt.receipts:
        return f"WORKER_RECEIPT_INCOMPLETE: attempt {n} lacks its WORKER_SWEEP or WORKER_CLEANUP"
    if failure := worker_failure(attempt):
        return f"WORKER_ACCOUNT_FAILED: {failure} (attempt {n})"
    if not sweep_clean(attempt):
        sweep = attempt.receipts[SWEEP]
        said = {k: sweep.get(k) for k in ("result", "wrapper", "left")}
        return f"WORKER_SWEEP_UNCLEAN: attempt {n} sweep {_shown(said)}"
    if attempt.receipts[CLEANUP].get("run_root_removed") is not True:
        root = attempt.receipts[LAUNCH]["run_root"]
        return f"WORKER_RUN_ROOT_LEFT: attempt {n} run root {root} was not removed"
    return None


def problems(receipts: Receipts) -> list[str]:
    """Every attempt that launched (or claims to) must show a clean, isolated run."""
    found = [] if receipts.known else ["WORKER_RECEIPTS_UNKNOWN: dispatcher stderr not all read"]
    user = receipts.run_as_user
    return found + [p for a in receipts.attempts.values() if (p := attempt_problem(a, user))]


def completion_problem(receipts: Receipts) -> str | None:
    """For a dispatcher that exited 0: its last line must be a ``completed`` status, and
    the attempt that status names must have run as the worker account."""
    final = receipts.final or {}
    if final.get("status") != "completed":
        return "DISPATCH_STATUS_MISSING: the last dispatcher line is not a completed status"
    attempt = receipts.attempts.get(json.dumps(final.get("attempts"), sort_keys=True))
    if attempt is None or LAUNCH not in attempt.receipts:
        n = _shown(final.get("attempts"))
        return f"WORKER_RECEIPT_MISSING: the completed attempt {n} has no WORKER_LAUNCH receipt"
    return None


def sweep_after_kill(dispatch, receipts: Receipts, *, killed: bool = True) -> list[str]:
    """On every exit path: sweep through the wrapper every launched run its receipts do
    not show swept and removed, also when other lines were lost. Returns what could not be
    shown gone. ``killed``: the dispatcher died by a signal, so a missing launch receipt
    means its launches are unknown (it may have died between launching and writing)."""
    unswept = [] if receipts.known else ["WORKER_RECEIPTS_UNKNOWN: some dispatcher lines lost"]
    if killed and not receipts.attempts:
        unswept.append("WORKER_RECEIPT_MISSING: no WORKER_LAUNCH before the kill; launches unknown")
    for attempt in receipts.attempts.values():
        if problem := launch_problem(attempt, receipts.run_as_user):
            unswept.append(problem)  # no trustworthy run root to sweep
        elif attempt_problem(attempt, receipts.run_as_user) is not None:
            unswept += _sweep_one(dispatch, receipts, attempt)
    return unswept


def _same_directory(root: str, run_root_id: object) -> bool:
    """The run root is still the directory the launch receipt's lstat named (dev, ino),
    so a post-hoc sweep never reaches another run that took the same path later."""
    try:
        info = os.lstat(root)
    except OSError:
        return False
    return run_root_id == [info.st_dev, info.st_ino]


def _sweep_one(dispatch, receipts: Receipts, attempt: Attempt) -> list[str]:
    launch = attempt.receipts[LAUNCH]
    root = launch["run_root"]
    if os.path.lexists(root):
        if not _same_directory(root, launch.get("run_root_id")):
            return [f"WORKER_SWEEP_UNCLEAN: {root} is not the directory its launch named"]
        clean = agy_dispatch.sweep_worker_run(dispatch, root)
        receipts.post_hoc[root] = "clean" if clean else "unclean"
        return [] if clean else [f"WORKER_SWEEP_UNCLEAN: {root} (wrapper sweep after the end)"]
    if sweep_clean(attempt):
        return []  # swept clean, then removed; only the cleanup line was cut off
    return [f"WORKER_SWEEP_UNCLEAN: {root} gone without a clean WORKER_SWEEP receipt"]


def record_path(log_path: Path) -> Path:
    return log_path.with_name(log_path.name.removesuffix(".log") + ".worker.json")


def write_record(log_path: Path, receipts: Receipts, found: list[str]) -> None:
    """The attempt's worker record (owner-only, never overwritten)."""
    final = receipts.final
    data = {
        "run_as_user": receipts.run_as_user,
        "receipts_complete": receipts.known,
        "final_status": {k: final.get(k) for k in ("status", "failure_kind", "attempts")}
        if final
        else None,
        "attempts": [_attempt_record(a) for a in receipts.attempts.values()],
        "post_hoc_sweeps": receipts.post_hoc,
        "problems": found,
        # at least one launched attempt, and nothing wrong with any of them
        "isolated": not found and any(LAUNCH in a.receipts for a in receipts.attempts.values()),
    }
    path = record_path(log_path)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError as exc:  # never overwrite: one record per attempt
        raise agy_dispatch.AgyWorkerAccountFailed(f"WORKER_RECORD_EXISTS: {path}") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        json.dump(data, out, indent=2, sort_keys=True, default=str)


def _attempt_record(attempt: Attempt) -> dict:
    launch, sweep, cleanup = (attempt.receipts.get(event) or {} for event in EVENTS)
    return {
        "attempt": attempt.number,
        "duplicate": attempt.duplicate,
        "run_as_user": launch.get("run_as_user"),
        "run_root": launch.get("run_root"),
        "run_root_id": launch.get("run_root_id"),
        "wrapper_sha256": launch.get("wrapper_sha256"),
        "sweep": {k: sweep.get(k) for k in ("result", "wrapper", "left")} if sweep else None,
        "run_root_removed": cleanup.get("run_root_removed"),
        "worker_failure": worker_failure(attempt),
    }
