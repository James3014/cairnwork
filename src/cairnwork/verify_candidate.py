"""Verify a candidate commit that Cairnwork's own worker did not make (``route verify``).

The candidate comes from elsewhere (another agent, CI). It gets the checks Cairnwork
applies to its own worker's output: one commit on the base that touches only the
allowed files (no deletion, symlink or submodule), the deferred tests laid over it,
the verifiers in the sandbox, and a Core verdict bound to the exact commit.

No worker runs and nothing is bound in the ownership ledger, so the deployer's
in-flight count never sees it. The run holds the shared run lock, as any run does,
so ``recover`` cannot mistake its verifier processes for a dead run's. The Core
record goes to the state's notary under a one-off ``verify-`` task id and attempt id.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from cairnwork import runlock
from cairnwork.deferred_tests import TestCommitRejected, lay_tests_over, never_seen
from cairnwork.gitcmd import OID
from cairnwork.intake import Intake
from cairnwork.ownership import request_digest
from cairnwork.pipeline import (
    TaskRejected,
    TaskResult,
    TaskSpec,
    check_task,
    judge_candidate,
    tests_with_teeth,
)
from cairnwork.workspace import (
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
    (the candidate, plus the deferred tests when given). Malformed input raises
    TaskRejected."""
    task = TaskSpec(
        f"verify-{secrets.token_hex(8)}", "verify", "verify", "", repo, base,
        allowed_files, verifiers, test_commit=test_commit,
    )  # fmt: skip
    specs = check_task(task)  # the same scope, protected-file and verifier rules
    if not OID.fullmatch(candidate):
        raise TaskRejected("BAD_CANDIDATE", "candidate must be a 40-hex commit id")
    intake.prepare()
    result = TaskResult(task.idempotency_key, f"a{secrets.token_hex(8)}", "FAILED")
    notary = intake.notary
    verifying = (intake.verifier_account(), intake.config()["verifier_python"])
    status = "FAILED"
    with runlock.held(intake.state / "ownership.sqlite", exclusive=False):
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
        remove_attempt_clone(run_root, result.attempt_id)
