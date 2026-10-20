"""One governed task, end to end: bind → isolated attempt → candidate → verify → Core.

``run_task`` is the bare core's main path. It owns no policy beyond the
preserved behaviors: every step that fails leaves the task FAILED (or not
COMPLETED) with a reason, and only a Core verdict that is verified for the
exact candidate commit makes it COMPLETED.

Any exception after binding is recorded as FAILED with an ``INFRA_ERROR``
reason; a crash of this process itself leaves the task DISPATCHED until restart
recovery (preserved behavior #21) exists. A FAILED task keeps its card: to try
again, move it to SUPERSEDED (``OwnershipLedger.set_status``) and submit with a
new idempotency key.

Workers are pluggable (``Worker``). A worker edits files in the clone it is
given; Cairnwork snapshots the allowed files into a commit itself (see
snapshot.py). The worker must have exited when ``run`` returns.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from cairnwork import runlock
from cairnwork.core_request import AcceptanceSpec
from cairnwork.gitcmd import OID
from cairnwork.journal import Journal
from cairnwork.notary import Notary
from cairnwork.ownership import OwnershipLedger
from cairnwork.snapshot import snapshot_candidate
from cairnwork.verifier_account import VerifierAccount
from cairnwork.verifiers import (
    VerifierRejected,
    VerifierSpec,
    parse_verifier,
    protected_verifier_inputs,
    run_verifiers,
)
from cairnwork.workspace import (
    CandidateRejected,
    WorkspaceError,
    create_attempt_clone,
    remove_attempt_clone,
)

FAULT_ENV = "CAIRNWORK_FAULT_AFTER"


class TaskRejected(Exception):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class TaskSpec:
    idempotency_key: str
    card_hash: str
    title: str
    instructions: str
    repo: Path
    base_commit: str
    allowed_files: tuple[str, ...]
    verifiers: tuple[tuple[str, tuple[str, ...]], ...]
    allow_deletions: bool = False

    def request(self) -> dict:
        return {
            "title": self.title,
            "instructions": self.instructions,
            "base_commit": self.base_commit,
            "allowed_files": sorted(self.allowed_files),
            "verifiers": [[vid, list(argv)] for vid, argv in self.verifiers],
            "allow_deletions": self.allow_deletions,
        }


class Worker(Protocol):
    """Edits the clone at workdir and returns when done.

    A worker that starts processes must give each one the environment variable
    CAIRNWORK_ATTEMPT=<workdir.name> (confined.ATTEMPT_ENV), so that restart
    recovery can find and stop them if Cairnwork dies mid-run (AgyWorker does).
    """

    def run(self, workdir: Path, task: TaskSpec) -> None: ...


@dataclass
class TaskResult:
    task_id: str
    attempt_id: str
    status: str
    reason: str = ""
    candidate_commit: str | None = None
    verified: bool = False
    logs: list[str] = field(default_factory=list)


def check_task(task: TaskSpec) -> tuple[VerifierSpec, ...]:
    """Reject a task before anything is bound or run."""
    if not isinstance(task.base_commit, str) or not OID.match(task.base_commit):
        raise TaskRejected("BAD_BASE", "base_commit must be a 40-hex commit id")
    if not task.allowed_files or len(set(task.allowed_files)) > 10:
        raise TaskRejected("BAD_SCOPE", "allowed_files must list 1 to 10 paths")
    if len(set(task.allowed_files)) != len(task.allowed_files):
        raise TaskRejected("BAD_SCOPE", "allowed_files has duplicates")
    for path in task.allowed_files:
        if not _clean_relative_path(path):
            raise TaskRejected("BAD_SCOPE", f"{path!r} is not a clean repository path")
    protected = protected_verifier_inputs(task.allowed_files)
    if protected:
        raise TaskRejected("PROTECTED_VERIFIER_INPUT", ", ".join(protected))
    try:
        return tuple(parse_verifier(vid, list(argv)) for vid, argv in task.verifiers)
    except VerifierRejected as exc:
        raise TaskRejected(exc.code, str(exc)) from exc


def _clean_relative_path(path: object) -> bool:
    if not isinstance(path, str) or not path or path != path.strip():
        return False
    parts = path.split("/")
    return (
        not path.startswith("/")
        and all(part not in ("", ".", "..") for part in parts)
        and all(part.lower() != ".git" for part in parts)
        and not any(ord(ch) < 32 or ch == "\\" for ch in path)
    )


def run_task(
    task: TaskSpec,
    *,
    ledger: OwnershipLedger,
    notary: Notary,
    journal: Journal,
    worker: Worker,
    run_root: Path,
    verifier_account: VerifierAccount | None = None,
) -> TaskResult:
    specs = check_task(task)
    with runlock.held(ledger.path, exclusive=False):
        bound = ledger.bind(task.idempotency_key, task.request(), "cairnwork", task.card_hash)
        record = bound.record
        if bound.duplicate:
            return TaskResult(record.task_id, "", record.status, reason="DUPLICATE_REQUEST")
        attempt_id = f"a{secrets.token_hex(8)}"
        steps = _Steps(journal, record.task_id, attempt_id)
        steps.reach("DISPATCHED", str(Path(run_root).resolve()))
        ledger.set_status(record.task_id, "DISPATCHED")
        result = TaskResult(record.task_id, attempt_id, "FAILED")
        try:
            _attempt(
                task,
                specs,
                record.request_digest,
                notary,
                worker,
                run_root,
                result,
                steps,
                verifier_account,
            )
        except Exception as exc:  # noqa: BLE001 - an infrastructure failure, never success
            result.verified = False
            result.reason = f"INFRA_ERROR: {type(exc).__name__}: {exc}"
        finally:
            result.status = "COMPLETED" if result.verified else "FAILED"
            steps.reach("FINISHED", result.status)
            ledger.set_status(record.task_id, result.status)
        return result


@dataclass(frozen=True)
class _Steps:
    """Journal each phase before doing it; CAIRNWORK_FAULT_AFTER=<phase> kills the process
    right after that journal write (crash drills only; a crash never yields success)."""

    journal: Journal
    task_id: str
    attempt_id: str

    def reach(self, phase: str, detail: str = "") -> None:
        self.journal.record(self.task_id, self.attempt_id, phase, detail)
        if os.environ.get(FAULT_ENV) == phase:
            os._exit(137)


def _attempt(
    task, specs, digest, notary, worker, run_root, result, steps, verifier_account
) -> None:
    task_id = steps.task_id
    attempt = create_attempt_clone(task.repo, task.base_commit, run_root, result.attempt_id)
    steps.reach("WORKER_STARTED")
    try:
        try:
            worker.run(attempt.path, task)
        except Exception as exc:  # noqa: BLE001 - any worker failure is a failed attempt
            result.reason = f"WORKER_FAILED: {type(exc).__name__}: {exc}"
            return
        try:
            candidate = snapshot_candidate(
                task.repo, attempt, task.allowed_files, allow_deletions=task.allow_deletions
            )
        except CandidateRejected as exc:
            result.reason = str(exc)
            return
        result.candidate_commit = candidate.candidate_commit
        steps.reach("CANDIDATE", candidate.candidate_commit)
        steps.reach("VERIFYING")
        runs = run_verifiers(
            task.repo,
            candidate.candidate_commit,
            specs,
            run_root,
            f"{result.attempt_id}-v",
            attempt_id=result.attempt_id,
            account=verifier_account,
        )
        result.logs = [str(run.log_path) for run in runs]
        spec = AcceptanceSpec(
            task_id,
            digest,
            tuple(s.verifier_id for s in specs),
            task.allowed_files,
            task.allow_deletions,
        )
        steps.reach("NOTARIZING")
        verdict = notary.notarize(
            task_id,
            result.attempt_id,
            spec,
            task.repo,
            task.base_commit,
            candidate.candidate_commit,
            tuple(run.observation for run in runs),
        )
        recorded = notary.verdict(
            task_id, result.attempt_id, expected_candidate=candidate.candidate_commit
        )
        result.verified = bool(verdict.verified and recorded and recorded.verified)
        result.reason = f"CORE_{verdict.status}"
    finally:
        try:
            remove_attempt_clone(run_root, result.attempt_id)
        except WorkspaceError as exc:  # keep the attempt's own outcome; note the leftover
            result.logs.append(f"cleanup failed: {exc}")
