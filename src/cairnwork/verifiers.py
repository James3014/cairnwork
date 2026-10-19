"""Run a task's verifiers on a fresh checkout of the candidate (preserved behaviors #5, #14).

Verifiers come from the task, so they are untrusted text. Only one shape is
accepted: ``python3 -m pytest [-q] [-p no:cacheprovider] tests/<file>.py[::<name>]...``.
Anything else is rejected before it runs, and nothing goes through a shell.

Each run happens in a new clone of the candidate commit made from Cairnwork's
repository (never in the worker's clone), with a minimal environment (no
GIT_*, no inherited credentials, HOME and TMPDIR inside the run directory,
bytecode and the pytest cache disabled) and a timeout. A verifier passes only
if it exits 0 and leaves the checkout identical to the candidate tree; a
verifier that writes into the checkout fails.

The candidate's code runs on this host. This is not a sandbox: a hostile test
file can still reach the network and the user's files. Running verifiers in a
sandbox is tracked in docs/PRESERVED_BEHAVIORS.md (open question under #14).
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from cairnwork.core_request import VerifierObservation
from cairnwork.treecompare import tree_differences
from cairnwork.workspace import WorkspaceError, create_attempt_clone, remove_attempt_clone

DEFAULT_TIMEOUT_SECONDS = 600
_TARGET = re.compile(r"^tests/(?:[A-Za-z0-9_\-]+/)*[A-Za-z0-9_\-]+\.py(?:::[A-Za-z0-9_\[\]\-]+)*$")
_OPTIONS = {("-q",), ("-p", "no:cacheprovider")}
_ENV_KEEP = ("PATH", "LANG", "LC_ALL")


class VerifierRejected(Exception):
    code = "VERIFIER_NOT_ALLOWED"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


@dataclass(frozen=True)
class VerifierSpec:
    verifier_id: str
    targets: tuple[str, ...]


@dataclass(frozen=True)
class VerifierRun:
    spec: VerifierSpec
    returncode: int | None
    timed_out: bool
    mutated: tuple[str, ...]
    log_path: Path
    observation: VerifierObservation


def parse_verifier(verifier_id: str, argv: list[str]) -> VerifierSpec:
    """Accept only python3 -m pytest with allowed options and tests/*.py targets."""
    if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
        raise VerifierRejected("argv must be a list of strings")
    if argv[:3] != ["python3", "-m", "pytest"]:
        raise VerifierRejected(f"{verifier_id}: only 'python3 -m pytest' is allowed")
    rest, targets = argv[3:], []
    while rest:
        if tuple(rest[:1]) in _OPTIONS:
            rest = rest[1:]
        elif tuple(rest[:2]) in _OPTIONS:
            rest = rest[2:]
        elif _TARGET.match(rest[0]):
            targets.append(rest.pop(0))
        else:
            raise VerifierRejected(f"{verifier_id}: argument {rest[0]!r} is not allowed")
    if not targets:
        raise VerifierRejected(f"{verifier_id}: no tests/*.py target")
    return VerifierSpec(verifier_id, tuple(targets))


def run_verifiers(
    repo: Path,
    candidate_commit: str,
    specs: tuple[VerifierSpec, ...],
    run_root: Path,
    run_id: str,
    *,
    python: str = sys.executable,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[VerifierRun, ...]:
    """Run each spec on a fresh checkout of candidate_commit; a failing test is not an error."""
    runs = []
    for index, spec in enumerate(specs):
        attempt_id = f"{run_id}-v{index}"
        checkout = create_attempt_clone(repo, candidate_commit, run_root, attempt_id).path
        try:
            runs.append(_run_one(repo, candidate_commit, spec, checkout, python, timeout))
        finally:
            try:
                remove_attempt_clone(run_root, attempt_id)
            except WorkspaceError:
                pass  # the run's verdict stands; a leftover directory is only disk space
    return tuple(runs)


def _run_one(repo, candidate_commit, spec, checkout, python, timeout) -> VerifierRun:
    home = checkout.parent / f"{checkout.name}.home"
    home.mkdir(exist_ok=True)
    env = {key: os.environ[key] for key in _ENV_KEEP if key in os.environ}
    env.update(HOME=str(home), TMPDIR=str(home), PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    argv = [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", *spec.targets]
    timed_out, returncode, output = False, None, b""
    try:
        done = subprocess.run(  # noqa: S603 - argv built from an allow-listed spec, no shell
            argv, cwd=checkout, env=env, capture_output=True, timeout=timeout, check=False
        )
        returncode, output = done.returncode, done.stdout + done.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out, output = True, (exc.stdout or b"") + (exc.stderr or b"")
    mutated = tuple(tree_differences(repo, candidate_commit, checkout))
    log_path = checkout.parent / f"{checkout.name}.log"
    log_path.write_bytes(output)
    passed = returncode == 0 and not timed_out and not mutated
    digest = hashlib.sha256(
        b"%s\0%r\0%r\0" % (" ".join(argv[1:]).encode(), returncode, mutated) + output
    ).hexdigest()
    observation = VerifierObservation(
        spec.verifier_id, f"{checkout.name}.log", f"sha256:{digest}", passed
    )
    return VerifierRun(spec, returncode, timed_out, mutated, log_path, observation)
