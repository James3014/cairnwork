import sqlite3
import threading

import pytest

from cairnwork.ownership import (
    DuplicateLogicalTask,
    InvalidOwnershipValue,
    KeyReusedWithDifferentRequest,
    OwnershipLedger,
    OwnershipUnknown,
    request_digest,
)

REQUEST = {"title": "fix off-by-one", "allowed_files": ["src/a.py"], "verify": ["pytest"]}


@pytest.fixture
def ledger(tmp_path):
    return OwnershipLedger(tmp_path / "ownership.sqlite")


def test_first_bind_creates_record_owned_by_requested_system(ledger):
    result = ledger.bind("key-1", REQUEST, "cairnwork", "card-1")
    assert not result.duplicate
    assert result.record.owner == "cairnwork"
    assert result.record.status == "BOUND"
    assert ledger.route(task_id=result.record.task_id) == "cairnwork"


def test_identical_resend_returns_the_same_task(ledger):
    first = ledger.bind("key-1", REQUEST, "cairnwork", "card-1")
    again = ledger.bind("key-1", dict(reversed(list(REQUEST.items()))), "cairnwork", "card-1")
    assert again.duplicate
    assert again.record == first.record


def test_resend_cannot_move_the_task_to_another_system(ledger):
    first = ledger.bind("key-1", REQUEST, "cairnwork", "card-1")
    again = ledger.bind("key-1", REQUEST, "nexus-new", "card-1")
    assert again.record.owner == "cairnwork"
    assert again.record.task_id == first.record.task_id


def test_same_key_with_different_request_is_rejected(ledger):
    ledger.bind("key-1", REQUEST, "cairnwork", "card-1")
    with pytest.raises(KeyReusedWithDifferentRequest):
        ledger.bind("key-1", {**REQUEST, "title": "other"}, "cairnwork", "card-1")


def test_second_task_for_an_active_card_is_rejected(ledger):
    ledger.bind("key-1", REQUEST, "cairnwork", "card-1")
    with pytest.raises(DuplicateLogicalTask):
        ledger.bind("key-2", REQUEST, "cairnwork", "card-1")


@pytest.mark.parametrize("status", ["CANCELLED", "SUPERSEDED"])
def test_card_can_be_rebound_after_its_task_is_inactive(ledger, status):
    first = ledger.bind("key-1", REQUEST, "cairnwork", "card-1")
    ledger.set_status(first.record.task_id, status)
    second = ledger.bind("key-2", REQUEST, "cairnwork", "card-1")
    assert second.record.task_id != first.record.task_id


def test_unknown_task_raises_instead_of_guessing_an_owner(ledger):
    with pytest.raises(OwnershipUnknown):
        ledger.route(task_id="task-missing")
    with pytest.raises(OwnershipUnknown):
        ledger.route(idempotency_key="key-missing")
    with pytest.raises(OwnershipUnknown):
        ledger.set_status("task-missing", "CANCELLED")


@pytest.mark.parametrize(
    "key,owner,card",
    [("", "cairnwork", "c"), ("k", "devspace", "c"), ("k", "cairnwork", " ")],
)
def test_invalid_inputs_never_create_a_record(ledger, key, owner, card):
    with pytest.raises(InvalidOwnershipValue):
        ledger.bind(key, REQUEST, owner, card)
    with pytest.raises(OwnershipUnknown):
        ledger.route(idempotency_key=key or "k")


def test_invalid_status_is_rejected(ledger):
    record = ledger.bind("key-1", REQUEST, "cairnwork", "card-1").record
    with pytest.raises(InvalidOwnershipValue):
        ledger.set_status(record.task_id, "DONE")
    assert ledger.get(task_id=record.task_id).status == "BOUND"


def test_binding_survives_reopening_the_ledger(tmp_path):
    path = tmp_path / "ownership.sqlite"
    record = OwnershipLedger(path).bind("key-1", REQUEST, "nexus-new", "card-1").record
    assert OwnershipLedger(path).get(idempotency_key="key-1") == record


def test_concurrent_binds_of_one_key_create_exactly_one_task(tmp_path):
    path = tmp_path / "ownership.sqlite"
    OwnershipLedger(path)
    results, errors = [], []

    def worker():
        try:
            results.append(OwnershipLedger(path).bind("key-1", REQUEST, "cairnwork", "card-1"))
        except Exception as exc:  # noqa: BLE001 - collected for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len({r.record.task_id for r in results}) == 1
    assert sum(not r.duplicate for r in results) == 1


def test_corrupt_ledger_fails_loudly(tmp_path):
    path = tmp_path / "ownership.sqlite"
    path.write_bytes(b"not a database" * 100)
    with pytest.raises(sqlite3.DatabaseError):
        OwnershipLedger(path)


def test_digest_ignores_key_order_but_not_values():
    assert request_digest({"a": 1, "b": 2}) == request_digest({"b": 2, "a": 1})
    assert request_digest({"a": 1}) != request_digest({"a": 2})
