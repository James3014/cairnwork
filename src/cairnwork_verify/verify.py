"""Verify a candidate commit made elsewhere (another agent, CI) with the checks Cairnwork
applies to its own worker's output: one commit on the base that touches only the allowed
files (no deletion, symlink or submodule), the deferred tests laid over it, the verifiers
in the sandbox, and a Core verdict bound to the exact commit.

No worker runs and nothing is bound in the ownership ledger, so the deployer's in-flight
count never sees it. The run holds the shared run lock, as any run does, so ``recover``
cannot mistake its verifier processes for a dead run's. Like ``submit`` it is refused
while the state is drained, and when the state names a ``mirror`` it works only in that
mirror (the base and the candidate are fetched into it from --repo-path). The Core record
goes to the state's notary under a one-off ``verify-`` task id.

A run marks itself with a locked file in ``<state>/verify-runs/``; the kernel drops the
lock when the run dies, so ``cleanup`` (also done at the start of every run) can tell a
dead run's leftovers (clones, baseline dirs, orphan processes) from a live run's.
"""

from __future__ import annotations

import fcntl
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from cairnwork import runlock
from cairnwork.confined import ATTEMPT_ENV, sweep_marked
from cairnwork.deferred_tests import TestCommitRejected, lay_tests_over, never_seen
from cairnwork.gitcmd import OID
from cairnwork.intake import Intake, IntakeRefused, mirror_base
from cairnwork.ownership import request_digest
from cairnwork.pipeline import (
    TaskRejected,
    TaskResult,
    TaskSpec,
    check_task,
    judge_candidate,
    tests_with_teeth,
)
from cairnwork.recovery import remove_leftovers
from cairnwork.workspace import (
    ATTEMPT_ID,
    Attempt,
    CandidateRejected,
    WorkspaceError,
    check_candidate_commit,
    create_attempt_clone,
    remove_attempt_clone,
)


def verify_candidate(
    intake: Intake,
    *,
    repo: Path,
    base: str,
    candidate: str,
    allowed_files: tuple[str, ...],
    verifiers: tuple[tuple[str, tuple[str, ...]], ...],
    test_commit: str = "",
) -> dict[str, Any]:
    """``verified`` is true only for a Core verdict bound to the commit that was judged
    (the candidate, plus the deferred tests when given). Raises TaskRejected for malformed
    input and IntakeRefused when drained or the mirror cannot be used."""
    task = TaskSpec(
        f"verify-{secrets.token_hex(8)}", "verify", "verify", "", repo, base,
        allowed_files, verifiers, test_commit=test_commit,
    )  # fmt: skip
    specs = check_task(task)  # the same scope, protected-file and verifier rules
    if not OID.fullmatch(candidate):
        raise TaskRejected("BAD_CANDIDATE", "candidate must be a 40-hex commit id")
    if not intake.accepting():
        raise IntakeRefused("VERIFY_NOT_ACCEPTING", "Cairnwork takes no new work (drained)")
    intake.prepare()
    task = _in_mirror(intake, task, candidate)
    cleanup(intake)
    result = TaskResult(task.idempotency_key, f"a{secrets.token_hex(8)}", "FAILED")
    notary = intake.notary
    verifying = (intake.verifier_account(), intake.config()["verifier_python"])
    status = "FAILED"
    with runlock.held(intake.state / "ownership.sqlite", exclusive=False):
        with _marked(intake, result.attempt_id):
            try:
                _judge(task, specs, candidate, notary, intake.run_root, result, verifying)
            except (CandidateRejected, TestCommitRejected, WorkspaceError) as exc:
                result.reason, status = str(exc), "REFUSED"
    judged = result.candidate_commit or candidate
    verdict = notary.verdict(result.task_id, result.attempt_id, expected_candidate=judged)
    return {
        "status": "VERIFIED" if result.verified else status,
        "verified": result.verified,
        "reason": result.reason,
        "core_status": verdict.status if verdict else "",
        "candidate_commit": candidate,
        "judged_commit": judged,
        "attempt_id": result.attempt_id,
        "logs": result.logs,
    }


def _in_mirror(intake: Intake, task: TaskSpec, candidate: str) -> TaskSpec:
    """With a configured mirror everything happens in the mirror, never in the caller's
    repository: fetch the base and the candidate into it, as submit does for the base."""
    mirror = intake.config()["mirror"]
    if not mirror:
        return task
    if not Path(mirror).is_dir():
        raise IntakeRefused("BAD_CONFIG", f"mirror {mirror} does not exist")
    if Path(mirror).resolve() != task.repo.resolve():
        for commit in (task.base_commit, candidate):
            mirror_base(Path(mirror), task.repo, commit)
    return TaskSpec(
        task.idempotency_key, task.card_hash, task.title, task.instructions, Path(mirror),
        task.base_commit, task.allowed_files, task.verifiers, test_commit=task.test_commit,
    )  # fmt: skip


def _judge(task, specs, candidate, notary, run_root, result, verifying) -> None:
    if task.test_commit:
        never_seen(task.repo, task.test_commit)
    clone = create_attempt_clone(task.repo, candidate, run_root, result.attempt_id)
    try:
        attempt = Attempt(clone.attempt_id, task.base_commit, clone.path, clone.refs_before)
        check_candidate_commit(task.repo, attempt, candidate, task.allowed_files)
        judged, test_files = candidate, ()
        if task.test_commit:
            test_files = tests_with_teeth(task, specs, run_root, result, verifying)
            judged = lay_tests_over(
                task.repo, judged, task.test_commit, test_files, result.attempt_id, run_root
            )
        result.candidate_commit = judged
        digest = request_digest(task.request() | {"candidate": candidate})
        judge_candidate(
            task, specs, digest, notary, run_root, result, test_files, verifying, lambda _: None
        )
    finally:
        try:
            remove_attempt_clone(run_root, result.attempt_id)
        except WorkspaceError as exc:  # keep the run's own outcome; note the leftover
            result.logs.append(f"cleanup failed: {exc}")


@contextmanager
def _marked(intake: Intake, attempt_id: str) -> Iterator[None]:
    """Hold a locked marker file for the duration of a run."""
    path = intake.state / "verify-runs" / attempt_id
    path.parent.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        path.unlink(missing_ok=True)
        os.close(fd)


def cleanup(intake: Intake) -> dict[str, Any]:
    """Remove what runs that died left behind: their orphan processes, clones and
    baseline directories. A run that is still alive holds its marker's lock and is left
    alone."""
    markers = intake.state / "verify-runs"
    cleaned: list[str] = []
    for marker in sorted(markers.iterdir()) if markers.is_dir() else []:
        if marker.is_symlink() or not ATTEMPT_ID.fullmatch(marker.name):
            continue
        fd = os.open(marker, os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            sweep_marked(ATTEMPT_ENV, marker.name)
            remove_leftovers(intake.run_root, marker.name)
            marker.unlink(missing_ok=True)
            cleaned.append(marker.name)
        except BlockingIOError:
            pass  # alive: it holds the lock
        finally:
            os.close(fd)
    return {"cleaned": cleaned}
