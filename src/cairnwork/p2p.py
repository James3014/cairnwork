"""Run existing (pass-to-pass) tests over a candidate next to the task's own verifiers (#117).

After the worker is done, for a task with deferred tests: select existing test files by the
state's ``p2p_selection`` mode (p2p_selection.py), run them on the BASE to find the tests that
pass there, then run exactly those on the judged commit as one more verifier (``p2p-<mode>``)
that Core must see pass. A test that already failed or was skipped on the base is not a
regression and is left out.

Failure paths, none of which can end VERIFIED:

* base probe times out (or cannot report) in ``whole-suite``: degrade to ``module-dir`` once and
  say so in the record; when ``module-dir`` itself cannot be probed the task is REFUSED;
* the candidate run times out or fails: its observation is not a pass, so Core says not verified;
* the p2p verifier id clashes with an author verifier: REFUSED.

``p2p_time_budget_seconds`` bounds each of the three runs (probe, degraded probe, candidate run).
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
from cairnwork.p2p_selection import select_p2p_targets
from cairnwork.verifiers import VerifierRun, VerifierSpec, run_verifiers

DEFAULT_BUDGET_SECONDS = 300


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
        if not files:
            return P2PPlan(rule | {"targets": 0}, None, budget)
        if files == tried:  # the narrower mode would run the same files again
            break
        outcomes, why = _probe(task, files, run_root, result, verifying, budget)
        if outcomes is not None:
            return _plan(task, specs, rule, _passing_targets(outcomes), budget)
        rule["degraded"], tried = why, files
    raise P2PRejected("P2P_BASE_PROBE_UNUSABLE", f"{rule['degraded']} in {rule['applied']} mode")


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
) -> tuple[VerifierRun, ...]:
    """The base-passing tests on the judged commit; each run notes the rule it used."""
    if plan.spec is None:
        return ()
    runs = run_verifiers(
        task.repo, judged, (plan.spec,), run_root, f"{result.attempt_id}-q",
        attempt_id=result.attempt_id, account=verifying[0], python=verifying[1],
        base_commit=task.base_commit, timeout=plan.timeout, note=plan.note,
    )  # fmt: skip
    for run in runs:
        try:
            (Path(run.log_path).parent / "p2p-selection.json").write_text(plan.note)
        except OSError:
            pass  # the hash already binds the rule
    return runs


def p2p_for(task, specs, test_files, run_root, result, verifying) -> P2PPlan | None:
    """The P2P plan of a task with deferred tests (p2p.py), computed once the worker is done."""
    if not task.test_commit:
        return None
    return plan_p2p(
        task, specs, test_files, task.p2p_mode, task.p2p_budget, run_root, result, verifying
    )
