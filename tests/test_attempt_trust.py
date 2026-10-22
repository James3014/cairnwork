"""Trust is re-checked before every attempt, not only before dispatch (PRESERVED #2, Issue #57)."""

# ruff: noqa: F811

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import commit_change  # noqa: F401
from test_pipeline import CrashWorker, EditWorker, project, stores, task  # noqa: F401

import cairnwork.attempt_gate as gate_module
import cairnwork.issue_runner as runner
from cairnwork.pipeline import TaskResult, _attempt, run_bound_task

pytestmark = pytest.mark.sandbox

SHA = "ab" * 32


def fake(**kw) -> Any:
    return SimpleNamespace(**kw)


def _bound(project, stores, tmp_path, worker, gate):
    ledger, notary, journal = stores
    spec = replace(task(project), before_attempt=gate)
    task_id = ledger.bind(spec.idempotency_key, spec.request(), "cairnwork", spec.card_hash)
    return run_bound_task(
        spec,
        task_id.record.task_id,
        ledger=ledger,
        notary=notary,
        journal=journal,
        worker=worker,
        run_root=tmp_path / "runs",
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
    monkeypatch.setattr(
        gate_module, "parse_task_block", lambda b, r: SimpleNamespace(block_sha256=SHA)
    )
    answers = iter(["", "someone-else"])
    github = fake(current=lambda number: {"body": "x"}, trust=lambda number, label: next(answers))
    gate = gate_module.attempt_gate(github, 7, SHA, ("tests",))
    spec, worker, reasons = replace(task(project), before_attempt=gate), CountingWorker(), []
    CountingWorker.runs = 0
    steps = SimpleNamespace(reach=lambda *a: None)
    for n in (1, 2):
        result = TaskResult("t", f"a{n}", "FAILED")
        _attempt(spec, (), "d", None, worker, tmp_path / "runs", result, steps, (None, None))
        reasons.append(result.reason)
    assert reasons[0].startswith("WORKER_FAILED")
    assert reasons[1] == runner.TRUST_CHANGED and CountingWorker.runs == 1


@pytest.mark.parametrize(
    "body_sha, trust, asked_trust",
    [("cd" * 32, "", False), (SHA, "AUTHOR_UNTRUSTED", True)],
    ids=["hash-mismatch", "distrust"],
)
def test_the_gate_refuses_a_changed_block_or_distrust(monkeypatch, body_sha, trust, asked_trust):
    monkeypatch.setattr(
        gate_module, "parse_task_block", lambda b, r: SimpleNamespace(block_sha256=body_sha)
    )
    asked = []
    github = fake(current=lambda n: {"body": "x"}, trust=lambda n, label: asked.append(1) or trust)
    assert gate_module.attempt_gate(github, 7, SHA, ("tests",))() == runner.TRUST_CHANGED
    assert bool(asked) is asked_trust  # a mismatch refuses before trust is asked; distrust asks


def test_the_gate_passes_an_unchanged_trusted_issue(monkeypatch):
    monkeypatch.setattr(
        gate_module, "parse_task_block", lambda b, r: SimpleNamespace(block_sha256=SHA)
    )
    github = fake(current=lambda n: {"body": "x"}, trust=lambda n, label: "")
    assert gate_module.attempt_gate(github, 7, SHA, ("tests",))() == ""


def test_the_gate_fails_closed_when_github_cannot_be_read():
    def boom(number):
        raise RuntimeError("gh down")

    github = fake(current=boom, trust=lambda n, label: "")
    assert gate_module.attempt_gate(github, 7, SHA, ("tests",))() == runner.TRUST_CHANGED


def _routed(monkeypatch, project, card_hash, gh=None):
    import cairnwork.route as route

    seen = {}
    monkeypatch.setattr(
        route,
        "run_bound_task",
        lambda *a, **k: seen.update(task=a[0]) or TaskResult("t", "a", "FAILED"),
    )
    monkeypatch.setattr(route, "agy_dispatch", lambda config: None)
    config = {
        "model": "m",
        "agy": "agy",
        "verifier_python": None,
        "test_roots": ["tests"],
        "gh": gh,
    }
    intake = fake(
        config=lambda: config,
        load_request=lambda tid: replace(task(project), card_hash=card_hash),
        verifier_account=lambda: None,
        ledger=None,
        notary=None,
        journal=None,
        run_root=None,
    )
    route._run(intake, "t1")
    return seen["task"].before_attempt


def test_the_runner_gates_an_issue_task_and_leaves_other_tasks_alone(monkeypatch, project):
    assert callable(_routed(monkeypatch, project, "gh:James3014/demo#7:" + SHA))
    assert _routed(monkeypatch, project, "card-1") is None
    assert _routed(monkeypatch, project, "gh:garbled")() == runner.TRUST_CHANGED


def test_a_card_naming_a_bad_repo_gets_a_refusing_gate_not_a_crash(monkeypatch, project):
    gate = _routed(monkeypatch, project, "gh:not a repo#7:" + SHA)
    assert gate() == runner.TRUST_CHANGED


def test_the_gate_uses_the_gh_the_issue_runner_was_started_with(monkeypatch, project):
    import cairnwork.route as route

    made = []
    real = route.GitHub
    monkeypatch.setattr(route, "GitHub", lambda gh, repo: made.append(gh) or real(gh, repo))
    _routed(monkeypatch, project, "gh:James3014/demo#7:" + SHA, gh="/opt/bot/gh")
    _routed(monkeypatch, project, "gh:James3014/demo#7:" + SHA)
    assert made == ["/opt/bot/gh", runner.DEFAULT_GH]


def test_the_issue_runner_records_its_gh_in_the_state_config(tmp_path):
    from cairnwork.intake import Intake

    intake = Intake(tmp_path / "state")
    intake.prepare()
    (intake.state / "config.json").write_text('{"model": "m"}')
    intake.remember_gh("/bin/sh")
    assert intake.config()["gh"] == "/bin/sh" and intake.config()["model"] == "m"


def test_a_config_without_test_roots_ends_the_task_refusing_not_stranded(monkeypatch, project):
    import cairnwork.route as route

    seen = {}
    monkeypatch.setattr(
        route, "run_bound_task", lambda *a, **k: seen.update(task=a[0]) or TaskResult("t", "a", "F")
    )
    monkeypatch.setattr(route, "agy_dispatch", lambda config: None)
    config = {"model": "m", "agy": "agy", "verifier_python": None, "gh": None}  # no test_roots
    intake = fake(
        config=lambda: config,
        load_request=lambda tid: replace(task(project), card_hash="gh:James3014/demo#7:" + SHA),
        verifier_account=lambda: None,
        ledger=None,
        notary=None,
        journal=None,
        run_root=None,
    )
    route._run(intake, "t1")
    assert callable(seen["task"].before_attempt)


def test_run_bound_task_gates_an_issue_task_that_arrives_without_a_gate(project, stores, tmp_path):
    seen = []

    class Spy:
        def run(self, workdir, task):
            seen.append(workdir)

    ledger, notary, journal = stores
    spec = replace(task(project), card_hash="gh:James3014/demo#7:" + SHA, before_attempt=None)
    bound = ledger.bind(spec.idempotency_key, spec.request(), "cairnwork", spec.card_hash)
    result = run_bound_task(
        spec, bound.record.task_id, ledger=ledger, notary=notary, journal=journal,
        worker=Spy(), run_root=tmp_path / "runs",
    )  # fmt: skip
    assert result.status == "FAILED" and result.reason == runner.TRUST_CHANGED and seen == []


def test_main_records_the_gh_it_ran_with(tmp_path, monkeypatch):
    import cairnwork.issue_runner as ir
    from cairnwork.intake import Intake

    recorded = []
    monkeypatch.setattr(Intake, "remember_gh", lambda self, gh: recorded.append(gh))
    monkeypatch.setattr(ir, "require_bot_gh", lambda gh, repo, clone: None)
    monkeypatch.setattr(ir, "_verify_origin", lambda clone, repo: None)
    monkeypatch.setattr(ir, "take", lambda *a: [])
    monkeypatch.setattr(ir, "settle", lambda *a: [])
    ir.main(["--state", str(tmp_path / "s"), "--gh", "/opt/bot/gh", "--repo", "a/b",
             "--clone", str(tmp_path), "run-once"])  # fmt: skip
    assert recorded == ["/opt/bot/gh"]


def test_a_relative_gh_in_the_state_config_is_bad_config(tmp_path):
    from cairnwork.intake import Intake, IntakeRefused

    intake = Intake(tmp_path / "state")
    intake.prepare()
    (intake.state / "config.json").write_text('{"gh": "gh"}')
    with pytest.raises(IntakeRefused) as caught:
        intake.config()
    assert caught.value.code == "BAD_CONFIG"


def test_remember_gh_skips_what_it_cannot_name_absolutely(tmp_path):
    from cairnwork.intake import Intake

    intake = Intake(tmp_path / "state")
    intake.prepare()
    (intake.state / "config.json").write_text('{"model": "m"}')
    intake.remember_gh("no-such-gh-binary-xyz")
    assert "gh" not in intake.config() or intake.config()["gh"] is None
