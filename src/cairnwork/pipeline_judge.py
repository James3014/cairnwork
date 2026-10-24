"""The candidate-judging phase of one attempt, split out of pipeline.py for the 400-line cap.

``tests_with_teeth`` checks a task's deferred tests really fail on the base; ``judge_candidate``
runs the verifiers over the candidate and has Core notarise it. Both take everything they need as
arguments (no module-level state) and are called by ``pipeline._attempt``; ``judge_candidate`` is
also shared with ``verify_candidate``. They never call back into ``pipeline``, so there is no cycle.
Behaviour is unchanged — this is a move, not a logic change.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from cairnwork.core_request import AcceptanceSpec
from cairnwork.deferred_tests import (
    BaseUncomparable,
    TestCommitRejected,
    base_comparison,
    check_roots_on_base,
    ensure_test_commit,
    new_tests,
    test_paths,
)
from cairnwork.p2p import P2PPlan, run_plan
from cairnwork.reasons import explain, verdict_reason
from cairnwork.verifiers import run_verifiers


def tests_with_teeth(task, specs, run_root, result, verifying) -> tuple[str, ...]:
    """Check a task's deferred tests once the worker is done: plain test files on the
    base whose test cases really fail there. Returns their paths; raises TestCommitRejected
    when they do not (NO_TEETH) and BaseUncomparable when the base cannot tell (#234)."""
    check_roots_on_base(task.repo, task.base_commit, task.test_roots)  # before any base run
    ensure_test_commit(task.repo, task.test_commit, task.test_store)
    paths = test_paths(task.repo, task.base_commit, task.test_commit, task.test_roots)
    targets = new_tests(task.repo, task.base_commit, task.test_commit, paths)  # may refuse
    runs = run_verifiers(
        task.repo,
        task.test_commit,
        specs,
        run_root,
        f"{result.attempt_id}-b",
        attempt_id=result.attempt_id,
        account=verifying[0],
        python=verifying[1],
        base_commit=task.base_commit,
        import_paths=task.import_paths,
    )
    comparison = base_comparison(runs, targets)  # decided before the base logs go
    for run in runs:  # not evidence for Core; would show the tests to later workers
        shutil.rmtree(Path(run.log_path).parent, ignore_errors=True)
    if comparison.kind == "UNCOMPARABLE":
        raise BaseUncomparable(comparison)
    if comparison.kind != "TEETH":
        raise TestCommitRejected(comparison.code, comparison.detail)
    return paths


def judge_candidate(
    task, specs, digest, notary, run_root, result, test_files, verifying, reach,
    p2p: P2PPlan | None = None, binding: dict | None = None,
) -> None:  # fmt: skip
    """Verify result.candidate_commit and have Core notarise it; sets result.verified,
    result.reason and result.logs. Shared by the worker pipeline and ``verify_candidate``;
    reach(phase) records progress. A binding (verify_candidate only, #229) binds the verdict to
    the tree that ran and to the binding's head instead of to result.candidate_commit."""
    judged = result.candidate_commit
    task_id = result.task_id
    reach("VERIFYING")
    runs = run_verifiers(
        task.repo,
        judged,
        specs,
        run_root,
        f"{result.attempt_id}-v",
        attempt_id=result.attempt_id,
        account=verifying[0],
        python=verifying[1],
        base_commit=task.base_commit,
        import_paths=task.import_paths,
    )
    if p2p is not None:
        p2p_runs, result.p2p = run_plan(p2p, task, judged, run_root, result, verifying)
        runs += p2p_runs
    result.logs = [str(run.log_path) for run in runs]
    spec = AcceptanceSpec(
        task_id,
        digest,
        tuple(run.spec.verifier_id for run in runs),
        tuple(task.allowed_files) + test_files,
        task.allow_deletions,
    )
    reach("NOTARIZING")
    verdict = notary.notarize(
        task_id,
        result.attempt_id,
        spec,
        task.repo,
        task.base_commit,
        judged,
        tuple(run.observation for run in runs),
        binding,
    )
    subject = binding["head"] if binding else judged
    recorded = notary.verdict(task_id, result.attempt_id, expected_candidate=subject)
    result.verified = bool(verdict.verified and recorded and recorded.verified)
    result.reason = verdict_reason(result.verified, verdict, recorded) + explain(runs)
