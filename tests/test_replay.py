"""Preserved behavior #8: the same request never runs twice, whatever key or card."""

import sqlite3
import threading

import pytest

from cairnwork.ownership import (
    DuplicateLogicalTask,
    OwnershipLedger,
    ReconciliationRequired,
    ReplayForbidden,
)

REQUEST = {"title": "fix off-by-one", "allowed_files": ["src/a.py"], "verify": ["pytest"]}


@pytest.fixture
def ledger(tmp_path):
    return OwnershipLedger(tmp_path / "ownership.sqlite")


def rows(ledger):
    conn = sqlite3.connect(ledger.path)
    try:
        return conn.execute("SELECT count(*) FROM task_ownership").fetchone()[0]
    finally:
        conn.close()


def finish(ledger, task_id, status):
    ledger.set_status(task_id, "DISPATCHED")
    ledger.set_status(task_id, status)


@pytest.mark.parametrize("status", ["COMPLETED", "FAILED"])
def test_finished_request_under_a_new_key_and_card_is_forbidden(ledger, status):
    first = ledger.bind("k1", REQUEST, "cairnwork", "card-1").record
    finish(ledger, first.task_id, status)
    with pytest.raises(ReplayForbidden, match="REPLAY_FORBIDDEN"):
        ledger.bind("k2", REQUEST, "cairnwork", "card-2")
    assert rows(ledger) == 1


def test_failed_request_may_be_resent_after_supersede(ledger):
    first = ledger.bind("k1", REQUEST, "cairnwork", "card-1").record
    finish(ledger, first.task_id, "FAILED")
    ledger.set_status(first.task_id, "SUPERSEDED")
    assert ledger.bind("k2", REQUEST, "cairnwork", "card-2").record.status == "BOUND"


@pytest.mark.parametrize("dispatch", [False, True])
def test_in_progress_request_needs_reconciliation(ledger, dispatch):
    first = ledger.bind("k1", REQUEST, "cairnwork", "card-1").record
    if dispatch:
        ledger.set_status(first.task_id, "DISPATCHED")
    with pytest.raises(ReconciliationRequired, match="RECONCILIATION_REQUIRED"):
        ledger.bind("k2", REQUEST, "cairnwork", "card-2")
    assert rows(ledger) == 1


def test_cancelled_request_may_be_resent_under_a_new_key(ledger):
    first = ledger.bind("k1", REQUEST, "cairnwork", "card-1").record
    ledger.set_status(first.task_id, "CANCELLED")
    assert ledger.bind("k2", REQUEST, "cairnwork", "card-2").record.status == "BOUND"


def test_different_request_and_same_key_resend_are_unaffected(ledger):
    first = ledger.bind("k1", REQUEST, "cairnwork", "card-1")
    assert ledger.bind("k1", REQUEST, "cairnwork", "card-1").duplicate
    other = ledger.bind("k2", {**REQUEST, "title": "other"}, "cairnwork", "card-2")
    assert other.record.task_id != first.record.task_id


def test_same_card_with_a_different_request_is_still_a_duplicate_task(ledger):
    ledger.bind("k1", REQUEST, "cairnwork", "card-1")
    with pytest.raises(DuplicateLogicalTask):
        ledger.bind("k2", {**REQUEST, "title": "other"}, "cairnwork", "card-1")


def test_concurrent_binds_of_one_request_with_different_keys_leave_one(tmp_path):
    path = tmp_path / "ownership.sqlite"
    OwnershipLedger(path)
    created, refused, other = [], [], []

    def worker(index):
        try:
            created.append(
                OwnershipLedger(path).bind(f"k{index}", REQUEST, "cairnwork", f"c{index}")
            )
        except ReconciliationRequired:
            refused.append(index)
        except Exception as exc:  # noqa: BLE001 - collected for the assertion below
            other.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert other == [] and len(created) == 1 and len(refused) == 7


def test_existing_state_with_two_rows_of_one_digest_still_opens(tmp_path):
    path = tmp_path / "ownership.sqlite"
    first = OwnershipLedger(path)
    conn = sqlite3.connect(path)
    for key, task, card in (("a", "t1", "c1"), ("b", "t2", "c2")):
        conn.execute(
            "INSERT INTO task_ownership VALUES (?, 'sha256:d', 'cairnwork', ?, ?, 'COMPLETED')",
            (key, task, card),
        )
    conn.commit()
    conn.close()
    assert OwnershipLedger(path).get(task_id="t2").status == "COMPLETED"
    assert first.path == path


def test_completed_replay_message_does_not_offer_supersede(ledger):
    first = ledger.bind("k1", REQUEST, "cairnwork", "card-1").record
    finish(ledger, first.task_id, "COMPLETED")
    with pytest.raises(ReplayForbidden) as raised:
        ledger.bind("k2", REQUEST, "cairnwork", "card-2")
    assert "supersede" not in str(raised.value)
