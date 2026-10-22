"""Trust is re-checked before every attempt, not only before dispatch (PRESERVED #2, Issue #57)."""

# ruff: noqa: F811

from types import SimpleNamespace
from typing import Any

import pytest
from conftest import commit_change  # noqa: F401
from test_pipeline import CrashWorker, EditWorker, project, stores, task  # noqa: F401

import cairnwork.issue_runner as runner
from cairnwork.pipeline import TaskResult, _attempt, run_bound_task

pytestmark = pytest.mark.sandbox

SHA = "ab" * 32


def fake(**kw) -> Any:
    return SimpleNamespace(**kw)


def _bound(project, stores, tmp_path, worker, gate):
    ledger, notary, journal = stores
    spec = task(project)
    task_id = ledger.bind(spec.idempotency_key, spec.request(), "cairnwork", spec.card_hash)
    return run_bound_task(
        spec,
        task_id.record.task_id,
        ledger=ledger,
        notary=notary,
        journal=journal,
        worker=worker,
        run_root=tmp_path / "runs",
        before_attempt=gate,
    )


class CountingWorker(CrashWorker):
    runs = 0

    def run(self, workdir, _task):
        type(self).runs += 1
        super().run(workdir, _task)


def test_a_refusing_gate_stops_the_attempt_before_the_worker(project, stores, tmp_path):
    seen = []

    class Spy:
        def run(self, workdir, task):
            seen.append(workdir)

    worker = Spy()
    result = _bound(project, stores, tmp_path, worker, lambda: runner.TRUST_CHANGED)
    assert result.status == "FAILED" and result.reason == runner.TRUST_CHANGED
    assert not seen


def test_a_passing_gate_is_asked_once_and_the_attempt_runs(project, stores, tmp_path):
    calls = []
    worker = EditWorker({"src/calc.py": "def add(a, b):\n    return a + b\n"})
    result = _bound(project, stores, tmp_path, worker, lambda: calls.append(1) or "")
    assert result.status == "COMPLETED" and calls == [1]


def test_identity_changed_between_attempt_one_and_two_refuses_attempt_two(
    project, tmp_path, monkeypatch
):
    monkeypatch.setattr(runner, "parse_task_block", lambda b, r: SimpleNamespace(block_sha256=SHA))
    answers = iter(["", "someone-else"])
    github = fake(current=lambda number: {"body": "x"}, trust=lambda number, label: next(answers))
    gate = runner.attempt_gate(github, 7, SHA, ("tests",))
    spec, worker, reasons = task(project), CountingWorker(), []
    CountingWorker.runs = 0
    steps = SimpleNamespace(reach=lambda *a: None)
    for n in (1, 2):
        result = TaskResult("t", f"a{n}", "FAILED")
        _attempt(spec, (), "d", None, worker, tmp_path / "runs", result, steps, (None, None), gate)
        reasons.append(result.reason)
    assert reasons[0].startswith("WORKER_FAILED")
    assert reasons[1] == runner.TRUST_CHANGED and CountingWorker.runs == 1


@pytest.mark.parametrize("body_sha, trust", [("cd" * 32, ""), (SHA, "AUTHOR_UNTRUSTED")])
def test_the_gate_refuses_a_changed_block_or_distrust(monkeypatch, body_sha, trust):
    monkeypatch.setattr(
        runner, "parse_task_block", lambda b, r: SimpleNamespace(block_sha256=body_sha)
    )
    github = fake(current=lambda n: {"body": "x"}, trust=lambda n, label: trust)
    assert runner.attempt_gate(github, 7, SHA, ("tests",))() == runner.TRUST_CHANGED


def test_the_gate_passes_an_unchanged_trusted_issue(monkeypatch):
    monkeypatch.setattr(runner, "parse_task_block", lambda b, r: SimpleNamespace(block_sha256=SHA))
    github = fake(current=lambda n: {"body": "x"}, trust=lambda n, label: "")
    assert runner.attempt_gate(github, 7, SHA, ("tests",))() == ""


def test_the_gate_fails_closed_when_github_cannot_be_read():
    def boom(number):
        raise RuntimeError("gh down")

    github = fake(current=boom, trust=lambda n, label: "")
    assert runner.attempt_gate(github, 7, SHA, ("tests",))() == runner.TRUST_CHANGED


def _routed(monkeypatch, card_hash):
    import cairnwork.route as route

    seen = {}
    monkeypatch.setattr(
        route, "run_bound_task", lambda *a, **k: seen.update(k) or TaskResult("t", "a", "FAILED")
    )
    monkeypatch.setattr(route, "agy_dispatch", lambda config: None)
    config = {"model": "m", "agy": "agy", "verifier_python": None, "test_roots": ["tests"]}
    intake = fake(
        config=lambda: config,
        load_request=lambda tid: SimpleNamespace(card_hash=card_hash),
        verifier_account=lambda: None,
        ledger=None,
        notary=None,
        journal=None,
        run_root=None,
    )
    route._run(intake, "t1")
    return seen["before_attempt"]


def test_the_runner_gates_an_issue_task_and_leaves_other_tasks_alone(monkeypatch):
    assert callable(_routed(monkeypatch, "gh:James3014/demo#7:" + SHA))
    assert _routed(monkeypatch, "card-1") is None
    assert _routed(monkeypatch, "gh:garbled")() == runner.TRUST_CHANGED
