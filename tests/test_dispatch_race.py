"""#243: a runner that read BOUND loses the race to a cancel; the two double-dispatch guards (#55).

A TLA+ pilot on main 148ab7f (knowledge nexus/research/2026-10-24-formal-verification-pilot)
found both: a cancel landing between ``run_bound_task``'s read and its dispatch escaped as a raw
InvalidStatusTransition and left a dangling DISPATCHED journal row; and the model needs at least
one of the two guards (journal unique index, ledger TRANSITIONS) to rule out a double dispatch.
"""

import threading

import pytest
from test_dispatch_once import BarrierLedger, CountingWorker, _run
from test_pipeline import project, task  # noqa: F401 - project is a fixture

import cairnwork.journal as journal_module
import cairnwork.ownership as ownership_module
from cairnwork.journal import Journal
from cairnwork.notary import Notary
from cairnwork.ownership import OWNER, OwnershipLedger
from cairnwork.recovery import recover

pytestmark = pytest.mark.sandbox


class ChangeAfterRead:
    """The runner's read sees BOUND; the given status lands right after it."""

    def __init__(self, ledger, status):
        self._ledger = ledger
        self._status = status

    def get(self, **kwargs):
        record = self._ledger.get(**kwargs)
        if record.status == "BOUND":
            self._ledger.set_status(record.task_id, self._status, expected_from="BOUND")
        return record

    def __getattr__(self, name):
        return getattr(self._ledger, name)


def _bind(ledger, spec):
    return ledger.bind(spec.idempotency_key, spec.request(), OWNER, spec.card_hash).record.task_id


def _stores(tmp_path):
    return (
        OwnershipLedger(tmp_path / "own.sqlite"),
        Notary(tmp_path / "notary.sqlite"),
        Journal(tmp_path / "journal.sqlite"),
    )


@pytest.mark.parametrize("status", ["CANCELLED", "SUPERSEDED"])
def test_a_cancel_between_read_and_dispatch_returns_not_bound(
    project,  # noqa: F811
    tmp_path,
    status,
):
    ledger, notary, journal = _stores(tmp_path)
    spec = task(project)
    task_id = _bind(ledger, spec)
    worker = CountingWorker()
    result = _run(spec, task_id, ChangeAfterRead(ledger, status), notary, journal, worker, tmp_path)
    assert (result.status, result.reason, result.verified) == (status, "NOT_BOUND", False)
    assert worker.calls == 0
    assert ledger.get(task_id=task_id).status == status
    entries = journal.entries(task_id)
    assert [(e.phase, e.detail) for e in entries][1:] == [("FINISHED", status)]
    assert entries[0].phase == "DISPATCHED"
    assert {e.attempt_id for e in entries} == {result.attempt_id}
    assert recover(ledger=ledger, notary=notary, journal=journal, run_root=tmp_path / "runs") == []
    assert not (tmp_path / "runs" / result.attempt_id).exists()


def _race_two_runners(project, tmp_path):  # noqa: F811 - the fixture's value
    ledger, notary, journal = _stores(tmp_path)
    spec = task(project)
    task_id = _bind(ledger, spec)
    worker, outcomes = CountingWorker(), []
    racing = BarrierLedger(ledger, 2)  # both runners read BOUND before either goes on

    def runner():
        try:
            outcomes.append(_run(spec, task_id, racing, notary, journal, worker, tmp_path))
        except Exception as exc:  # noqa: BLE001 - recorded, the test asserts there is none
            outcomes.append(exc)

    threads = [threading.Thread(target=runner) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    dispatched = sum(e.phase == "DISPATCHED" for e in journal.entries(task_id))
    return worker.calls, dispatched, outcomes


@pytest.mark.parametrize(
    ("journal_index", "ledger_transitions", "worker_runs"),
    [
        (True, True, 1),
        (False, True, 1),  # the ledger alone refuses DISPATCHED -> DISPATCHED
        (True, False, 1),  # the journal alone refuses a second DISPATCHED row
        (False, False, 2),  # neither: both runners dispatch (TLA+ mutant M2)
    ],
)
def test_at_least_one_double_dispatch_guard_is_load_bearing(
    project,  # noqa: F811
    tmp_path,
    monkeypatch,
    journal_index,
    ledger_transitions,
    worker_runs,
):
    """Mutation check (#55, #243): remove the journal's one-DISPATCHED-row index, the ledger's
    TRANSITIONS check, or both. Either guard alone keeps one dispatch; without both a bound task
    runs twice. A refactor may drop one of them, never both."""
    if not journal_index:
        monkeypatch.setattr(journal_module, "_ONE_DISPATCH", "SELECT 1")
    if not ledger_transitions:
        anything = frozenset(ownership_module.TRANSITIONS)
        loose = {status: anything for status in ownership_module.TRANSITIONS}
        monkeypatch.setattr(ownership_module, "TRANSITIONS", loose)
    calls, dispatched, outcomes = _race_two_runners(project, tmp_path)
    assert not [o for o in outcomes if isinstance(o, Exception)], outcomes
    assert calls == worker_runs
    assert dispatched == (1 if journal_index else 2)
    not_bound = sorted(o.reason == "NOT_BOUND" for o in outcomes)
    assert not_bound == ([False, False] if worker_runs == 2 else [False, True])
