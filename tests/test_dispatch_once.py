"""Preserved behaviors #8/#9: a task is dispatched once; a dispatched task never reruns."""

import threading

import pytest
from test_pipeline import project, task  # noqa: F401 - project is a fixture

from cairnwork.journal import Journal
from cairnwork.notary import Notary
from cairnwork.ownership import OWNER, OwnershipLedger
from cairnwork.pipeline import run_bound_task
from cairnwork.recovery import recover

pytestmark = pytest.mark.sandbox


class CountingWorker:
    def __init__(self):
        self.calls = 0
        self._lock = threading.Lock()

    def run(self, workdir, _task):
        with self._lock:
            self.calls += 1
        raise RuntimeError("no change needed for this test")


class BarrierLedger:
    """Holds each runner's FIRST status read until all of them have read BOUND."""

    def __init__(self, ledger, parties):
        self._ledger = ledger
        self._barrier = threading.Barrier(parties, timeout=30)
        self._seen = threading.local()

    def get(self, **kwargs):
        record = self._ledger.get(**kwargs)
        if not getattr(self._seen, "waited", False):
            self._seen.waited = True
            self._barrier.wait()
        return record

    def __getattr__(self, name):
        return getattr(self._ledger, name)


@pytest.fixture
def stores(tmp_path):
    return (
        OwnershipLedger(tmp_path / "own.sqlite"),
        Notary(tmp_path / "notary.sqlite"),
        Journal(tmp_path / "journal.sqlite"),
    )


def _run(spec, task_id, ledger, notary, journal, worker, tmp_path):
    return run_bound_task(
        spec,
        task_id,
        ledger=ledger,
        notary=notary,
        journal=journal,
        worker=worker,
        run_root=tmp_path / "runs",
    )


def test_two_runners_on_one_bound_task_dispatch_once(project, stores, tmp_path):  # noqa: F811
    ledger, notary, journal = stores
    spec = task(project)
    bound = ledger.bind(spec.idempotency_key, spec.request(), OWNER, spec.card_hash)
    worker, outcomes = CountingWorker(), []
    racing = BarrierLedger(ledger, 2)
    task_id = bound.record.task_id

    def runner():
        try:
            outcomes.append(_run(spec, task_id, racing, notary, journal, worker, tmp_path))
        except Exception as exc:  # noqa: BLE001 - the loser may be refused, never run
            outcomes.append(exc)

    threads = [threading.Thread(target=runner) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    entries = journal.entries(bound.record.task_id)
    assert not [o for o in outcomes if isinstance(o, Exception)], outcomes
    assert sorted(o.reason == "NOT_BOUND" for o in outcomes) == [False, True]
    assert worker.calls == 1
    assert len({e.attempt_id for e in entries}) == 1
    assert sum(e.phase == "DISPATCHED" for e in entries) == 1


def test_a_dispatched_journal_row_with_a_bound_ledger_is_not_rerun_and_recovers(
    project,  # noqa: F811
    stores,
    tmp_path,
):
    """Crash shape: the journal row is written, the ledger move to DISPATCHED is lost."""
    ledger, notary, journal = stores
    spec = task(project)
    bound = ledger.bind(spec.idempotency_key, spec.request(), OWNER, spec.card_hash)
    journal.record(bound.record.task_id, "a0123456789abcdef", "DISPATCHED", str(tmp_path / "runs"))
    worker = CountingWorker()
    result = _run(spec, bound.record.task_id, ledger, notary, journal, worker, tmp_path)
    assert result.reason == "NOT_BOUND" and result.status == "BOUND"
    assert worker.calls == 0
    decisions = recover(ledger=ledger, notary=notary, journal=journal, run_root=tmp_path / "runs")
    assert [(d.new_status, d.reason) for d in decisions] == [
        ("FAILED", "INTERRUPTED_AFTER_DISPATCHED")
    ]
    assert ledger.get(task_id=bound.record.task_id).status == "FAILED"


@pytest.mark.parametrize("status", ["DISPATCHED", "COMPLETED", "FAILED"])
def test_run_bound_task_on_a_non_bound_task_never_calls_the_worker(
    project,  # noqa: F811
    stores,
    tmp_path,
    status,
):
    ledger, notary, journal = stores
    spec = task(project)
    bound = ledger.bind(spec.idempotency_key, spec.request(), OWNER, spec.card_hash)
    ledger.set_status(bound.record.task_id, "DISPATCHED")
    if status != "DISPATCHED":
        ledger.set_status(bound.record.task_id, status)
    worker = CountingWorker()
    result = _run(spec, bound.record.task_id, ledger, notary, journal, worker, tmp_path)
    assert result.reason == "NOT_BOUND" and result.status == status
    assert worker.calls == 0
    assert journal.entries(bound.record.task_id) == []
