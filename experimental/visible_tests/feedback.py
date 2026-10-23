"""Let a worker see its visible tests before it hands back (Issue #32; experimental, default off).

The Agy worker runs headless and cannot run commands, so it edits blind. ``VisibleTestsWorker``
wraps any worker (pipeline.Worker): after the inner worker's turn, Cairnwork itself runs the
task's *visible* verification commands on the edit and, if they do not all pass, gives the inner
worker another turn (``retries``, default one) with a short pass/fail summary appended to its
instructions. The pipeline then judges the final clone exactly as without the wrapper.

What the probe reuses and what the worker can see:

* The edit is turned into a commit by ``snapshot.snapshot_candidate`` (the same code that makes the
  real candidate, so no git runs in the worker's clone) and run with ``verifiers.run_verifiers`` (a
  fresh checkout in the Seatbelt sandbox, optional verifier account; no hand-rolled profile).
* Only verifiers whose every target file exists at the **base commit** are run. A task's deferred
  tests (``test_commit``) are never fetched, laid over or named: the probe touches only the base,
  the edit and Cairnwork's own repository, and the deferred commit stays unfetched until the
  pipeline needs it after the worker is done (deferred_tests.never_seen still holds).
* The worker gets only the summary: per visible verifier, passed or not, the fixed-vocabulary cause
  (reasons.why_not_passed) and the names of the tests that did not pass, cleaned and capped. No log
  bytes. The probe's logs and checkouts are deleted, and its attempt ref removed, before the next
  turn starts.

A probe failure (sandbox, account, git) propagates, so the pipeline records the attempt as
WORKER_FAILED rather than silently judging an unprobed edit. Not wired into any runner: a run that
wants it builds ``VisibleTestsWorker(AgyWorker(...))`` itself; core never imports this package.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from cairnwork.gitcmd import GitCommandError, run_git
from cairnwork.snapshot import snapshot_candidate
from cairnwork.verifier_account import VerifierAccount
from cairnwork.verifiers import (
    DEFAULT_TIMEOUT_SECONDS,
    VerifierRejected,
    VerifierRun,
    VerifierSpec,
    parse_verifier,
    run_verifiers,
)
from cairnwork.workspace import (
    ATTEMPT_REF_PREFIX,
    Attempt,
    CandidateRejected,
    _guarded_refs,
)

MAX_NAMES = 10
MAX_NAME_CHARS = 160
# Control and bidi characters plus angle brackets: test names are written by the code under test
# and must not be able to open or close a fence in the worker's prompt.
_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f  ‎‏‪-‮⁦-⁩<>]")
AGY_LOG_SUFFIX = ".agy.log"  # AgyWorker opens <run_root>/<attempt>.agy.log with O_EXCL per turn

HEADER = (
    "Cairnwork ran this repository's visible verification tests on your edit, in its sandbox "
    "(no network). They did not all pass:"
)
HIDDEN = (
    "Other tests that will judge your fix are not in this repository; passing the tests above "
    "is necessary, not sufficient."
)
FOOTER = (
    "Your previous edits are still in the files. Change only the allowed files so these tests "
    "pass; do not modify tests and do not run commands."
)


@dataclass(frozen=True)
class VisibleTestsWorker:
    """A pipeline.Worker that wraps ``inner`` with the visible-test feedback loop."""

    inner: Any
    retries: int = 1
    python: str | None = None  # the verifier Python (state config verifier_python)
    account: VerifierAccount | None = None
    timeout: int = DEFAULT_TIMEOUT_SECONDS

    def run(self, workdir: Path, task) -> None:
        self.inner.run(workdir, task)
        specs = visible_specs(task)
        if not specs:
            return
        for turn in range(1, self.retries + 1):
            feedback = self.probe(workdir, task, specs, turn)
            if feedback is None:
                return
            _set_aside_log(workdir, turn)
            self.inner.run(
                workdir, replace(task, instructions=f"{task.instructions}\n\n{feedback}")
            )

    def probe(self, workdir: Path, task, specs: tuple[VerifierSpec, ...], turn: int) -> str | None:
        """Run the visible specs on the clone's current edit; None when they all pass."""
        attempt = Attempt(
            workdir.name, task.base_commit, workdir, _guarded_refs(task.repo, workdir.name)
        )
        ref = ATTEMPT_REF_PREFIX + workdir.name
        root = workdir.parent / f"{workdir.name}-visible{turn}"
        try:
            try:
                candidate = snapshot_candidate(
                    task.repo, attempt, task.allowed_files, allow_deletions=task.allow_deletions
                )
            except CandidateRejected as exc:
                return f"Cairnwork could not check your edit: {_why_rejected(exc.code)}\n{FOOTER}"
            root.mkdir(mode=0o700)
            runs = run_verifiers(
                task.repo,
                candidate.candidate_commit,
                specs,
                root,
                f"{workdir.name}-vis{turn}",
                python=self.python,
                timeout=self.timeout,
                attempt_id=workdir.name,
                account=self.account,
                base_commit=task.base_commit,
                import_paths=task.import_paths,
            )
            try:
                return summarize(runs, hidden=bool(task.test_commit))
            finally:
                self._drop_logs(runs)
        finally:
            run_git(task.repo, "update-ref", "-d", ref)
            shutil.rmtree(root, ignore_errors=True)

    def _drop_logs(self, runs: tuple[VerifierRun, ...]) -> None:
        for run in runs:
            private = run.log_path.parent
            if self.account is not None:
                self.account.remove(private)
            shutil.rmtree(private, ignore_errors=True)


def visible_specs(task) -> tuple[VerifierSpec, ...]:
    """The task's verifiers whose every target file is in the base commit, in task order."""
    out = []
    for verifier_id, argv in task.verifiers:
        try:
            spec = parse_verifier(verifier_id, list(argv), task.test_roots)
        except VerifierRejected:
            continue  # the pipeline rejects the task itself; nothing to probe
        if all(_in_base(task.repo, task.base_commit, t.split("::", 1)[0]) for t in spec.targets):
            out.append(spec)
    return tuple(out)


def _in_base(repo: Path, base: str, path: str) -> bool:
    try:
        run_git(repo, "cat-file", "-e", f"{base}:{path}")
    except GitCommandError:
        return False
    return True


def summarize(runs: tuple[VerifierRun, ...], *, hidden: bool) -> str | None:
    """The worker-facing summary, or None when every run passed. Never reads a log."""
    if all(run.observation.passed for run in runs):
        return None
    lines = [HEADER]
    for run in runs:
        vid = _clean(run.spec.verifier_id)
        if run.observation.passed:
            lines.append(f"- {vid}: passed")
            continue
        line = f"- {vid}: did not pass ({_clean(run.why) or 'no cause recorded'})"
        bad = sorted((n, o) for n, o in run.outcomes.items() if o != "passed")
        if bad:
            shown = ", ".join(f"{_clean(n)} ({_clean(o)})" for n, o in bad[:MAX_NAMES])
            more = f", +{len(bad) - MAX_NAMES} more" if len(bad) > MAX_NAMES else ""
            line += f"; tests not passing: {shown}{more}"
        lines.append(line)
    if hidden:
        lines.append(HIDDEN)
    lines.append(FOOTER)
    return "\n".join(lines)


def _clean(text: str) -> str:
    return _UNSAFE.sub("?", str(text))[:MAX_NAME_CHARS]


def _why_rejected(code: str) -> str:
    if code == "NO_COMMIT":
        return "you changed none of the allowed files."
    return f"the edit was refused ({_clean(code)}); keep changes to the allowed files only."


def _set_aside_log(workdir: Path, turn: int) -> None:
    """Move the inner worker's log of the previous turn aside so the next turn can create its own
    (AgyWorker refuses to reuse one); Cairnwork's run root, never the worker's clone."""
    log = workdir.parent / f"{workdir.name}{AGY_LOG_SUFFIX}"
    kept = workdir.parent / f"{workdir.name}.turn{turn}{AGY_LOG_SUFFIX}"
    if log.is_file() and not log.is_symlink() and not os.path.lexists(kept):
        os.rename(log, kept)
