"""Run existing (pass-to-pass) tests over a candidate next to the task's own verifiers (#117).

After the worker is done, for a task with deferred tests: select existing test files by the
state's ``p2p_selection`` mode (p2p_selection.py), run them on the BASE to find the tests that
pass there, then run exactly those on the judged commit as one more verifier (``p2p-<mode>``)
that Core must see pass. A test that already failed or was skipped on the base is not a
regression and is left out.

Failure paths, none of which can end VERIFIED:

* base probe times out (or cannot report) in ``whole-suite``: degrade to ``module-dir`` once and
  say so in the record; when ``module-dir`` itself cannot be probed, or selects no test file at
  all, or none of its tests passes on the base, the task is REFUSED (a degraded probe never
  turns into "nothing to run");
* the candidate run times out: its observation is not a pass, so Core says not verified;
* the candidate run fails only because a test failed (``VerifierRun.plain_failure``: no timeout,
  mutated checkout, surviving process or oversized log, and the guard reported a failed test):
  it is re-run once (flaky rule). A pass on the re-run is what Core sees, with the first run's
  failing tests and integrity signals recorded in the rule (``rerun``) and ``p2p_rerun_passed``
  set, so a clean pass and a pass after re-run can be told apart (the verdict is the same); a
  second failure is what Core sees, so not verified. Any other failed run blocks as it is,
  without a re-run;
* the p2p verifier id clashes with an author verifier: REFUSED.

``p2p_time_budget_seconds`` bounds each run (probe, degraded probe, candidate run, re-run).
The rule actually used is a dict (``P2PPlan.rule``): it goes into the p2p observation's artifact
hash (so Core's record binds it), into ``p2p-selection.json`` beside the verifier log, and into the
task result. Core's notarisation contract is unchanged.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from posixpath import basename

from cairnwork.deferred_tests import TestCommitRejected
from cairnwork.gitcmd import GitCommandError, run_git
from cairnwork.issue_task import test_target
from cairnwork.p2p_selection import DEFAULT_BUDGET_SECONDS, select_p2p_targets
from cairnwork.verifiers import VerifierRun, VerifierSpec, run_verifiers


class P2PRejected(TestCommitRejected):
    """The P2P step could not run safely: the task is REFUSED, never verified."""


@dataclass(frozen=True)
class P2PPlan:
    rule: dict
    spec: VerifierSpec | None  # None: nothing to run (mode off, or no base-passing test)
    timeout: int = DEFAULT_BUDGET_SECONDS

    @property
    def note(self) -> str:
        """What is bound into the observation's artifact hash."""
        return json.dumps(self.rule, sort_keys=True)


def _base_tests(task, exclude: frozenset[str]) -> tuple[str, ...]:
    """Test files (test_*.py, *_test.py) under the roots on the base that are plain targets."""
    try:
        raw = run_git(task.repo, "ls-tree", "-r", "-z", "--name-only", task.base_commit,
                      "--", *task.test_roots)  # fmt: skip
    except GitCommandError as exc:
        raise P2PRejected("P2P_BASE_UNREADABLE", str(exc)) from exc
    target = test_target(task.test_roots)
    return tuple(
        sorted(
            p for p in raw.split("\0")
            if p and p not in exclude and target.fullmatch(p)
            and (basename(p).startswith("test_") or p.endswith("_test.py"))
        )
    )  # fmt: skip


def _probe(task, files, run_root, result, verifying, budget):
    """Per-test outcomes of ``files`` on the base, or (None, reason) when the run is unusable."""
    runs = run_verifiers(
        task.repo, task.base_commit, (VerifierSpec("p2p-probe", files),), run_root,
        f"{result.attempt_id}-p", attempt_id=result.attempt_id, account=verifying[0],
        python=verifying[1], base_commit=task.base_commit, timeout=budget,
    )  # fmt: skip
    for run in runs:  # base evidence is not for Core
        shutil.rmtree(Path(run.log_path).parent, ignore_errors=True)
    run = runs[0]
    if run.timed_out:
        return None, "base-probe-timeout"
    if run.returncode not in (0, 1) or not run.outcomes:
        return None, "base-probe-failed"
    return run.outcomes, ""


def _passing_targets(outcomes: dict[str, str]) -> tuple[str, ...]:
    """A file whose every test passed on the base is a target by itself; in a file with other
    outcomes only the test functions (all parametrisations passed) are."""
    by_file: dict[str, dict[str, bool]] = {}
    for nodeid, outcome in outcomes.items():
        path = nodeid.split("::", 1)[0]
        func = nodeid.split("[", 1)[0]
        funcs = by_file.setdefault(path, {})
        funcs[func] = funcs.get(func, True) and outcome == "passed"
    targets: list[str] = []
    for path, funcs in sorted(by_file.items()):
        if all(funcs.values()):
            targets.append(path)
        else:
            targets.extend(sorted(f for f, ok in funcs.items() if ok))
    return tuple(targets)


def plan_p2p(task, specs, test_files, mode, budget, run_root, result, verifying) -> P2PPlan:
    """Select and probe; see the module docstring. ``test_files`` (the deferred tests) are never
    P2P tests: the author's verifiers cover them."""
    rule: dict = {"configured": mode, "applied": mode, "budget_seconds": budget, "degraded": ""}
    if mode == "off":
        return P2PPlan(rule, None, budget)
    existing = _base_tests(task, frozenset(test_files))
    tried: tuple[str, ...] | None = None
    for applied in (mode, "module-dir") if mode == "whole-suite" else (mode,):
        files = select_p2p_targets(task.allowed_files, existing, task.test_roots, applied)
        rule |= {"applied": applied, "selected_files": len(files)}
        if not files and rule["degraded"]:  # #117 F1: a failed probe is not "nothing to run"
            raise _unusable(rule, "no module-dir test to fall back on")
        if not files:
            return P2PPlan(rule | {"targets": 0}, None, budget)
        if files == tried:  # the narrower mode would run the same files again
            break
        outcomes, why = _probe(task, files, run_root, result, verifying, budget)
        if outcomes is not None:
            plan = _plan(task, specs, rule, _passing_targets(outcomes), budget)
            if plan.spec is None and rule["degraded"]:  # G1: and none passes on the base
                raise _unusable(rule, "no module-dir test passes on the base")
            return plan
        rule["degraded"], tried = why, files
    raise P2PRejected("P2P_BASE_PROBE_UNUSABLE", f"{rule['degraded']} in {rule['applied']} mode")


def _unusable(rule: dict, why: str) -> P2PRejected:
    return P2PRejected("P2P_BASE_PROBE_UNUSABLE", f"{rule['degraded']}; {why}")


def _plan(task, specs, rule, targets, budget) -> P2PPlan:
    digest = hashlib.sha256("\n".join(targets).encode()).hexdigest()
    rule = rule | {"targets": len(targets), "targets_sha256": digest}
    if not targets:
        return P2PPlan(rule, None, budget)
    verifier_id = f"p2p-{rule['applied']}"
    if verifier_id in {spec.verifier_id for spec in specs}:
        raise P2PRejected("P2P_VERIFIER_ID_CLASH", verifier_id)
    return P2PPlan(rule, VerifierSpec(verifier_id, targets), budget)


def run_plan(
    plan: P2PPlan, task, judged: str, run_root, result, verifying
) -> tuple[tuple[VerifierRun, ...], dict]:
    """The base-passing tests on the judged commit, and the rule actually used. A run that fails
    without timing out is re-run once; Core sees the re-run, whose note records the first run's
    failures (``rerun``), so a flaky pass is not blocking but is bound into the record."""
    if plan.spec is None:
        return (), plan.rule
    run = _run_once(plan.spec, plan, plan.note, task, judged, run_root, result, verifying, "q")
    if run.observation.passed or not run.plain_failure:  # timeout, mutation, survivors...: block
        return (run,), plan.rule
    failing = sorted(_clean(n) for n, outcome in run.outcomes.items() if outcome != "passed")
    first = {"failed": failing, "returncode": run.returncode, "timed_out": run.timed_out,
             "mutated": list(run.mutated), "survivors": run.survivors, "ran": run.ran}  # fmt: skip
    rule = plan.rule | {"rerun": first}
    note = json.dumps(rule, sort_keys=True)
    run = _run_once(plan.spec, plan, note, task, judged, run_root, result, verifying, "r")
    # Not in the note: the flag is the re-run's outcome, which the note is hashed before.
    return (run,), rule | {"p2p_rerun_passed": bool(run.observation.passed)}


def _clean(name: str) -> str:
    """A test id for the record: printable, bounded."""
    return "".join(c if c.isprintable() else "?" for c in str(name))[:200]


def _run_once(spec, plan, note, task, judged, run_root, result, verifying, tag) -> VerifierRun:
    (run,) = run_verifiers(
        task.repo, judged, (spec,), run_root, f"{result.attempt_id}-{tag}",
        attempt_id=result.attempt_id, account=verifying[0], python=verifying[1],
        base_commit=task.base_commit, timeout=plan.timeout, note=note,
    )  # fmt: skip
    try:
        (Path(run.log_path).parent / "p2p-selection.json").write_text(note)
    except OSError:
        pass  # the hash already binds the rule
    return run


def p2p_for(task, specs, test_files, run_root, result, verifying) -> P2PPlan | None:
    """The P2P plan of a task with deferred tests (p2p.py), computed once the worker is done."""
    if not task.test_commit:
        return None
    return plan_p2p(
        task, specs, test_files, task.p2p_mode, task.p2p_budget, run_root, result, verifying
    )
