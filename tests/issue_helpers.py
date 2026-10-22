"""Shared by the Issue runner tests: the notarized attempt a settled task stands on (#61)."""

import subprocess

from cairnwork.core_request import AcceptanceSpec, VerifierObservation

ATTEMPT = "a" + "1" * 16


def notarize(setup, taken, commit, attempt=ATTEMPT, passed=True):
    """What the pipeline did before the Issue was settled: one notarized attempt for commit."""
    request = setup["intake"].load_request(taken["task_id"])
    diff = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", request.base_commit, commit],
        cwd=setup["clone"], capture_output=True, text=True, check=True,
    ).stdout.split()  # fmt: skip
    spec = AcceptanceSpec(taken["task_id"], "sha256:" + "c" * 64, ("pytest",), tuple(diff))
    obs = (VerifierObservation("pytest", "artifact-1", "sha256:" + "e" * 64, passed),)
    setup["intake"].notary.notarize(
        taken["task_id"], attempt, spec, setup["clone"], request.base_commit, commit, obs
    )


def ensure_notarized(setup, taken, commit):
    if not setup["intake"].notary.verdict(taken["task_id"], ATTEMPT, expected_candidate=commit):
        notarize(setup, taken, commit)


def _completed(setup, taken, commit, notarized=True):
    """The COMPLETED status for commit; notarized=False leaves the notary to the caller."""
    if notarized:
        ensure_notarized(setup, taken, commit)
    return lambda self, **kw: {
        "task_id": taken["task_id"],
        "status": "COMPLETED",
        "core_verified": True,
        "attempt_id": ATTEMPT,
        "candidate_commit": commit,
        "detail": "",
    }
