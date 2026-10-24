"""The usage record of the visible-test wrapper (experimental, Issue #234): extra turns that
actually ran and a fixed feedback source, next to the attempt, with no candidate-controlled text."""

import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from test_visible_tests import (
    CALC_FIX,
    CALC_WRONG,
    FakeAccount,
    TurnWorker,
    _run,
    _verifier,
    _with_account,
    project,  # noqa: F401 - fixture reused from test_visible_tests
)

import experimental.visible_tests.feedback as feedback
from experimental.visible_tests import VisibleTestsWorker


@dataclass(frozen=True)
class Task:
    instructions: str = "fix it"


class Quiet:
    def __init__(self):
        self.turns = 0

    def run(self, workdir, task) -> None:
        self.turns += 1


def _fake_probe(monkeypatch, outcomes):
    """probe() answers outcomes[turn-1]: a summary string (still failing) or None (all pass)."""
    monkeypatch.setattr(feedback, "visible_specs", lambda task: (object(),))
    monkeypatch.setattr(
        VisibleTestsWorker, "probe", lambda self, wd, task, specs, turn: outcomes[turn - 1]
    )


def _record(workdir):
    path = workdir.parent / f"{workdir.name}.visible-tests.json"
    return json.loads(path.read_text())


def _go(tmp_path, worker):
    workdir = tmp_path / "attempt-1"
    workdir.mkdir(exist_ok=True)
    worker.run(workdir, Task())
    return _record(workdir)


def test_all_pass_after_the_first_turn_records_zero_extra_turns(tmp_path, monkeypatch):
    _fake_probe(monkeypatch, [None])
    inner = Quiet()
    rec = _go(tmp_path, VisibleTestsWorker(inner, retries=3, pass_fail_only=True))
    assert inner.turns == 1
    assert rec == {
        "schema": 1,
        "feedback_source": "visible-tests:pass-fail-only",
        "retries": 3,
        "extra_turns": 0,
    }


@pytest.mark.parametrize("retries", [1, 2, 3])
def test_still_failing_after_n_records_n_extra_turns(tmp_path, monkeypatch, retries):
    _fake_probe(monkeypatch, ["x"] * retries)
    inner = Quiet()
    rec = _go(tmp_path, VisibleTestsWorker(inner, retries=retries, pass_fail_only=True))
    assert inner.turns == 1 + retries and rec["extra_turns"] == retries
    assert rec["retries"] == retries


def test_passing_on_the_second_probe_records_one_extra_turn(tmp_path, monkeypatch):
    _fake_probe(monkeypatch, ["x", None])
    inner = Quiet()
    rec = _go(tmp_path, VisibleTestsWorker(inner, retries=3, pass_fail_only=True))
    assert inner.turns == 2 and rec["extra_turns"] == 1


def test_no_visible_specs_still_records_zero_extra_turns(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "visible_specs", lambda task: ())
    rec = _go(tmp_path, VisibleTestsWorker(Quiet(), retries=2, pass_fail_only=True))
    assert rec["extra_turns"] == 0


def test_the_source_follows_the_mode(tmp_path, monkeypatch):
    _fake_probe(monkeypatch, [None])
    account = SimpleNamespace(run_root=tmp_path / "acct")
    rec = _go(tmp_path, VisibleTestsWorker(Quiet(), retries=1, account=account))
    assert rec["feedback_source"] == "visible-tests:account-detail"
    rec = _go(tmp_path, VisibleTestsWorker(Quiet(), retries=1, pass_fail_only=True))
    assert rec["feedback_source"] == "visible-tests:pass-fail-only"
    # an account with pass_fail_only still shows no test text, so it is the fixed-wording source
    both = VisibleTestsWorker(Quiet(), retries=1, account=account, pass_fail_only=True)
    assert _go(tmp_path, both)["feedback_source"] == "visible-tests:pass-fail-only"


def test_a_probe_that_raises_still_leaves_the_turns_that_ran(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "visible_specs", lambda task: (object(),))

    def boom(self, wd, task, specs, turn):
        if turn == 1:
            return "x"
        raise RuntimeError("probe failed")

    monkeypatch.setattr(VisibleTestsWorker, "probe", boom)
    workdir = tmp_path / "attempt-1"
    workdir.mkdir()
    worker = VisibleTestsWorker(Quiet(), retries=3, pass_fail_only=True)
    with pytest.raises(RuntimeError):
        worker.run(workdir, Task())
    assert _record(workdir)["extra_turns"] == 1


@pytest.mark.sandbox
@pytest.mark.parametrize("with_account", [False, True])
def test_a_real_run_records_counts_and_no_test_names(
    project,  # noqa: F811
    tmp_path,
    monkeypatch,
    with_account,  # noqa: F811
):
    inner = TurnWorker([CALC_WRONG, CALC_WRONG, CALC_FIX])
    account: FakeAccount | None = None
    if with_account:
        account = FakeAccount((tmp_path / "account-runs").resolve())
        _with_account(monkeypatch, account)  # routes run_verifiers into the account's root

    def wrap(w):
        if account is not None:
            return VisibleTestsWorker(w, retries=2, account=account)
        return VisibleTestsWorker(w, retries=2, pass_fail_only=True)

    result = _run(
        project, tmp_path, inner, (_verifier("pytest-0", "tests/test_sub.py"),), wrap=wrap
    )
    assert result.status == "COMPLETED", result.reason
    records = list((tmp_path / "state" / "runs").glob("*.visible-tests.json"))
    assert len(records) == 1
    text = records[0].read_text()
    rec = json.loads(text)
    assert rec["extra_turns"] == 2 and rec["retries"] == 2
    assert set(rec) == {"schema", "feedback_source", "retries", "extra_turns"}
    expected = "account-detail" if with_account else "pass-fail-only"
    assert rec["feedback_source"] == f"visible-tests:{expected}"
    for leaked in ("test_sub", "pytest-0", "calc", "tests/"):
        assert leaked not in text
