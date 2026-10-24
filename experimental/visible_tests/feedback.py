"""Let a worker see its visible tests before it hands back (Issue #32; experimental, default off).

The Agy worker runs headless and cannot run commands, so it edits blind. ``VisibleTestsWorker``
wraps any worker (pipeline.Worker): after the inner worker's turn, Cairnwork itself runs the
task's *visible* verification commands on the edit and, if they do not all pass, gives the inner
worker another turn (up to ``retries`` of them) with a short pass/fail summary appended to its
instructions. The pipeline then judges the final clone exactly as without the wrapper.

Explicit opt-in only (council action 2, Issue #234): the product itself never retries, and the
A2 arm was not shown worth its cost (its one A/B interval included 0). So ``retries`` has no
default: a caller must pass a positive int (``VISIBLE_TESTS_NEEDS_RETRIES`` otherwise).

Usage record: every run of the wrapper writes ``<run_root>/<attempt>.visible-tests.json`` (beside
the clone and the ``-visible{turn}`` roots, never inside the clone), so the run's owner can see
afterwards whether and how the feedback was used. Fields, all counts or fixed strings and no
candidate-controlled text (no test, verifier or file names):

* ``schema``: 1
* ``feedback_source``: ``"visible-tests:pass-fail-only"`` or ``"visible-tests:account-detail"``
* ``retries``: the extra turns allowed
* ``extra_turns``: extra turns started (0..retries); a turn whose inner run raises still counts

What the probe reuses and what the worker can see:

* The edit is turned into a commit by ``snapshot.snapshot_candidate`` (the same code that makes the
  real candidate, so no git runs in the worker's clone) and run with ``verifiers.run_verifiers`` (a
  fresh checkout in the Seatbelt sandbox; no hand-rolled profile).
* Only verifiers whose every target file exists at the **base commit** are run. A task's deferred
  tests (``test_commit``) are never fetched, laid over or named: the probe touches only the base,
  the edit and Cairnwork's own repository (deferred_tests.never_seen still holds afterwards).
* The code under test runs inside the pytest process and can rewrite the guard's report, so every
  test name and outcome in it is **candidate-controlled text**. Without a VerifierAccount that
  code also runs as the owner's uid and can read other same-user processes' argv and environment
  (verifiers.py, "Known gap"), so any text it chooses could carry secrets into the next prompt.
  Hence:

  - no account: the wrapper refuses to be built unless ``pass_fail_only=True`` is passed, and then
    the summary is fixed wording only (verifier id from the task, passed or not; nothing else);
  - with an account: a test name is shown only if it starts with one of that verifier's target
    files and matches a strict ASCII charset; outcomes only from a fixed set; the cause only as
    fixed phrases (changed file names dropped); everything else is counted, never quoted.
  Residual channel in account mode (Issue #207, measured in the PR #204 round-2 review): the code
  under test can still choose names that pass the filter (charset and target-file prefix), so
  each turn's summary can carry up to MAX_NAMES x MAX_NAME_CHARS = 10 x 160 characters of text of
  its choosing. In the round-2 review's measurements that text could not carry other processes'
  secrets on this host: reading another uid's KERN_PROCARGS2 is EPERM for a non-root account,
  the setuid /bin/ps is denied under the Seatbelt profile, and require_idle keeps other account
  processes away while a probe runs. The only payload demonstrated was this run's own one-time
  nonce, which the run already holds; this is a measured result, not a proof.
  Still open before promotion (Issue #207): a check with a real verifier account, not the
  same-uid stand-in used in CI; and showing only node ids that the base version collects (one
  extra collect; judged not worth its cost, see the Issue).
* No log bytes ever reach the summary. The probe's checkouts and log directories (also under the
  account's run root, also when a run raises midway) and its attempt ref are removed before the
  next turn starts.

A probe failure (sandbox, account, git) propagates, so the pipeline records the attempt as
WORKER_FAILED rather than silently judging an unprobed edit. Not wired into any runner: a run
that wants it builds ``VisibleTestsWorker(AgyWorker(...), retries=1, account=...)`` itself; core
never imports this package.
"""

from __future__ import annotations

import json
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
SAFE_NAME = re.compile(r"[A-Za-z0-9_./:\[\]-]{1," + str(MAX_NAME_CHARS) + "}")
SHOWN_OUTCOMES = frozenset({"failed", "error", "skipped", "xfailed", "xpassed"})
# The fixed phrases of reasons.why_not_passed; anything else in a cause is counted, not quoted.
FIXED_CAUSES = (
    re.compile(r"no sandbox backend: verifier not executed"),
    re.compile(r"timed out"),
    re.compile(r"\d{1,6} process\(es\) outlived the run, or the process list was unreadable"),
    re.compile(r"log over the size cap"),
    re.compile(r"test report over the size cap"),
    re.compile(r"pytest exit code -?\d{1,4}"),
    re.compile(r"pytest passed but the guard saw no test run"),
)
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
NEEDS_RETRIES = (
    "VISIBLE_TESTS_NEEDS_RETRIES: retries must be passed explicitly as a positive int; "
    "visible-test feedback is an explicit opt-in"
)
SOURCE_PASS_FAIL = "visible-tests:pass-fail-only"
SOURCE_DETAIL = "visible-tests:account-detail"
RECORD_SUFFIX = ".visible-tests.json"
NEEDS_ACCOUNT = (
    "VISIBLE_TESTS_NEEDS_ACCOUNT: without a verifier account the code under test can read "
    "same-user processes; pass account=... or pass_fail_only=True"
)


@dataclass(frozen=True)
class VisibleTestsWorker:
    """A pipeline.Worker that wraps ``inner`` with the visible-test feedback loop."""

    inner: Any
    retries: Any = None  # required: a positive int (None means "not passed")
    python: str | None = None  # the verifier Python (state config verifier_python)
    account: VerifierAccount | None = None
    timeout: int = DEFAULT_TIMEOUT_SECONDS
    pass_fail_only: bool = False  # required without an account: fixed wording, no test text

    def __post_init__(self) -> None:
        if type(self.retries) is not int or self.retries <= 0:
            raise ValueError(NEEDS_RETRIES)
        if self.account is None and not self.pass_fail_only:
            raise ValueError(NEEDS_ACCOUNT)

    def run(self, workdir: Path, task) -> None:
        extra = 0
        try:
            self.inner.run(workdir, task)
            specs = visible_specs(task)
            if not specs:
                return
            # Each extra turn gets the original instructions plus only the latest summary: the
            # files already hold the earlier turns' work, and the prompt does not grow per turn.
            for turn in range(1, self.retries + 1):
                feedback = self.probe(workdir, task, specs, turn)
                if feedback is None:
                    return
                _set_aside_log(workdir, turn)
                extra += 1
                self.inner.run(
                    workdir, replace(task, instructions=f"{task.instructions}\n\n{feedback}")
                )
        finally:
            self._write_record(workdir, extra)

    def _write_record(self, workdir: Path, extra: int) -> None:
        detail = self.account is not None and not self.pass_fail_only
        record = {
            "schema": 1,
            "feedback_source": SOURCE_DETAIL if detail else SOURCE_PASS_FAIL,
            "retries": self.retries,
            "extra_turns": extra,
        }
        path = workdir.parent / f"{workdir.name}{RECORD_SUFFIX}"
        tmp = path.with_name(path.name + ".tmp")
        if os.path.lexists(tmp):
            os.unlink(tmp)  # a leftover from a failed write; never followed if it is a symlink
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write(json.dumps(record, sort_keys=True) + "\n")
        os.replace(tmp, path)

    def probe(self, workdir: Path, task, specs: tuple[VerifierSpec, ...], turn: int) -> str | None:
        """Run the visible specs on the clone's current edit; None when they all pass."""
        attempt = Attempt(
            workdir.name, task.base_commit, workdir, _guarded_refs(task.repo, workdir.name)
        )
        root = workdir.parent / f"{workdir.name}-visible{turn}"
        run_id = f"{workdir.name}-vis{turn}"
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
                run_id,
                python=self.python,
                timeout=self.timeout,
                attempt_id=workdir.name,
                account=self.account,
                base_commit=task.base_commit,
                import_paths=task.import_paths,
            )
            detail = self.account is not None and not self.pass_fail_only
            return summarize(runs, hidden=bool(task.test_commit), detail=detail)
        finally:
            self._clean_up(task.repo, ATTEMPT_REF_PREFIX + workdir.name, root, run_id)

    def _clean_up(self, repo: Path, ref: str, root: Path, run_id: str) -> None:
        """Each step runs even if an earlier one raises; the first error is re-raised."""
        try:
            if self.account is not None:
                _sweep(self.account.run_root, run_id, self.account)
        finally:
            try:
                shutil.rmtree(root, ignore_errors=True)
            finally:
                run_git(repo, "update-ref", "-d", ref)


def _sweep(run_root: Path, run_id: str, account: VerifierAccount) -> None:
    """Remove every checkout and log directory of this probe from the account's run root,
    including those of runs that finished before a later one raised. Only names that are exactly
    this probe's (``<run_id>-v<n>`` then ``.`` or end), so another probe's are never touched. Each
    entry is tried even if an earlier one fails; the first error is re-raised afterwards. A
    symlink is unlinked, never followed (account.remove skips symlinks by design)."""
    if not run_root.is_dir():
        return
    mine = re.compile(rf"{re.escape(run_id)}-v\d+(\.|$)")
    first: BaseException | None = None
    for entry in run_root.iterdir():
        if not mine.match(entry.name):
            continue
        try:
            if entry.is_symlink():
                entry.unlink()
            else:
                account.remove(entry)
                shutil.rmtree(entry, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001 - keep sweeping; re-raised below
            first = first or exc
    if first is not None:
        raise first


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


def summarize(runs: tuple[VerifierRun, ...], *, hidden: bool, detail: bool) -> str | None:
    """The worker-facing summary, or None when every run passed. Never reads a log; with
    ``detail`` False nothing the code under test wrote is used (only the pass bit)."""
    if all(run.observation.passed for run in runs):
        return None
    lines = [HEADER]
    for run in runs:
        vid = _safe(run.spec.verifier_id) or "verifier"
        if run.observation.passed:
            lines.append(f"- {vid}: passed")
        elif not detail:
            lines.append(f"- {vid}: did not pass")
        else:
            lines.append(f"- {vid}: did not pass ({_cause(run.why)}){_tests(run)}")
    if hidden:
        lines.append(HIDDEN)
    lines.append(FOOTER)
    return "\n".join(lines)


def _tests(run: VerifierRun) -> str:
    """Shown: names under one of the spec's target files, in the safe charset, with a fixed
    outcome; every other non-passing result is only counted."""
    files = {t.split("::", 1)[0] for t in run.spec.targets}
    shown, other = [], 0
    for name, outcome in sorted(run.outcomes.items()):
        if outcome == "passed":
            continue
        ok = outcome in SHOWN_OUTCOMES and _safe(name) and name.split("::", 1)[0] in files
        if ok and len(shown) < MAX_NAMES:
            shown.append(f"{name} ({outcome})")
        else:
            other += 1
    text = f"; tests not passing: {', '.join(shown)}" if shown else ""
    return text + (f"; {other} other result(s) not shown" if other else "")


def _cause(why: str) -> str:
    parts, other = [], 0
    for part in why.split("; ") if why else ():
        if part.startswith("checkout changed ("):
            parts.append("checkout changed")
        elif any(p.fullmatch(part) for p in FIXED_CAUSES):
            parts.append(part)
        else:
            other += 1
    if other:
        parts.append(f"{other} other cause(s)")
    return "; ".join(parts) or "no cause recorded"


def _safe(text: str) -> str:
    """``text`` if it is entirely in the safe ASCII charset and short enough, else ""."""
    text = str(text)
    return text if SAFE_NAME.fullmatch(text) else ""


def _why_rejected(code: str) -> str:
    if code == "NO_COMMIT":
        return "you changed none of the allowed files."
    return f"the edit was refused ({_safe(code) or 'refused'}); keep changes to the allowed files."


def _set_aside_log(workdir: Path, turn: int) -> None:
    """Move the inner worker's log of the previous turn aside so the next turn can create its own
    (AgyWorker refuses to reuse one); Cairnwork's run root, never the worker's clone."""
    log = workdir.parent / f"{workdir.name}{AGY_LOG_SUFFIX}"
    kept = workdir.parent / f"{workdir.name}.turn{turn}{AGY_LOG_SUFFIX}"
    if log.is_file() and not log.is_symlink() and not os.path.lexists(kept):
        os.rename(log, kept)
