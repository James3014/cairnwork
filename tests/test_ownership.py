import sqlite3
import threading

import pytest

from cairnwork.ownership import (
    DuplicateLogicalTask,
    InvalidOwnershipValue,
    InvalidStatusTransition,
    KeyReusedWithDifferentRequest,
    LedgerIntegrityViolation,
    OwnerMismatch,
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
    with pytest.raises(OwnerMismatch):
        ledger.bind("key-1", REQUEST, "nexus-new", "card-1")
    assert ledger.route(task_id=first.record.task_id) == "cairnwork"


def test_resend_aimed_at_another_card_is_rejected(ledger):
    ledger.bind("key-1", REQUEST, "cairnwork", "card-1")
    with pytest.raises(KeyReusedWithDifferentRequest):
        ledger.bind("key-1", REQUEST, "cairnwork", "card-2")


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
        ledger.route(idempotency_key=key.strip() or "k")


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


def test_cancelled_task_cannot_be_revived_while_a_newer_task_holds_the_card(ledger):
    old = ledger.bind("key-1", REQUEST, "cairnwork", "card-1").record
    ledger.set_status(old.task_id, "CANCELLED")
    ledger.bind("key-2", REQUEST, "cairnwork", "card-1")
    for status in ("BOUND", "DISPATCHED", "COMPLETED"):
        with pytest.raises(InvalidStatusTransition):
            ledger.set_status(old.task_id, status)
    assert ledger.get(task_id=old.task_id).status == "CANCELLED"


@pytest.mark.parametrize("terminal", ["COMPLETED", "CANCELLED", "SUPERSEDED"])
def test_terminal_statuses_cannot_be_left(ledger, terminal):
    record = ledger.bind("key-1", REQUEST, "cairnwork", "card-1").record
    if terminal == "COMPLETED":
        ledger.set_status(record.task_id, "DISPATCHED")
    ledger.set_status(record.task_id, terminal)
    with pytest.raises(InvalidStatusTransition):
        ledger.set_status(record.task_id, "BOUND")


def test_failed_task_keeps_the_card_until_superseded(ledger):
    failed = ledger.bind("key-1", REQUEST, "cairnwork", "card-1").record
    ledger.set_status(failed.task_id, "FAILED")
    with pytest.raises(DuplicateLogicalTask):
        ledger.bind("key-2", REQUEST, "cairnwork", "card-1")
    ledger.set_status(failed.task_id, "SUPERSEDED")
    retry = ledger.bind("key-2", REQUEST, "cairnwork", "card-1").record
    assert retry.task_id != failed.task_id


def test_schema_blocks_two_active_tasks_per_card_even_for_raw_writes(tmp_path):
    path = tmp_path / "ownership.sqlite"
    OwnershipLedger(path).bind("key-1", REQUEST, "cairnwork", "card-1")
    conn = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO task_ownership VALUES ('key-2', 'd', 'cairnwork', 't2', 'card-1', 'BOUND')"
        )
    conn.close()


@pytest.mark.parametrize(
    "bad",
    [None, ["a"], {1: "a"}, {"a": float("nan")}, {"a": {1, 2}}, {"a": b"x"}],
)
def test_digest_rejects_non_json_requests(bad):
    with pytest.raises(InvalidOwnershipValue):
        request_digest(bad)


def test_concurrent_binds_of_one_card_with_different_keys_leave_one_active(tmp_path):
    path = tmp_path / "ownership.sqlite"
    OwnershipLedger(path)
    created, rejected, other = [], [], []

    def worker(index):
        try:
            created.append(OwnershipLedger(path).bind(f"k{index}", REQUEST, "cairnwork", "c"))
        except DuplicateLogicalTask:
            rejected.append(index)
        except Exception as exc:  # noqa: BLE001 - collected for the assertion below
            other.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert other == []
    assert len(created) == 1
    assert len(rejected) == 7


def test_existing_ledger_with_two_active_tasks_per_card_fails_with_a_code(tmp_path):
    path = tmp_path / "ownership.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE task_ownership (idempotency_key TEXT PRIMARY KEY, request_digest TEXT,"
        " owner TEXT, task_id TEXT UNIQUE, card_hash TEXT, status TEXT)"
    )
    conn.execute("INSERT INTO task_ownership VALUES ('a','d','cairnwork','t1','c','BOUND')")
    conn.execute("INSERT INTO task_ownership VALUES ('b','d','cairnwork','t2','c','BOUND')")
    conn.commit()
    conn.close()
    with pytest.raises(LedgerIntegrityViolation):
        OwnershipLedger(path)


def test_schema_rejects_unknown_status_values(tmp_path):
    path = tmp_path / "ownership.sqlite"
    OwnershipLedger(path)
    conn = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO task_ownership VALUES ('k','d','cairnwork','t','c','cancelled')")
    conn.close()


def test_failed_transaction_is_rolled_back_and_releases_the_lock(tmp_path):
    # The write lock must be free after a failed set_status. Either the explicit
    # ROLLBACK or closing the connection releases it; both are intended.
    path = tmp_path / "ownership.sqlite"
    ledger = OwnershipLedger(path)
    record = ledger.bind("key-1", REQUEST, "cairnwork", "card-1").record
    ledger.set_status(record.task_id, "CANCELLED")
    with pytest.raises(InvalidStatusTransition):
        ledger.set_status(record.task_id, "BOUND")
    with pytest.raises(OwnershipUnknown):
        ledger.set_status("task-missing", "CANCELLED")
    conn = sqlite3.connect(path, timeout=0)
    conn.execute("BEGIN IMMEDIATE")  # would raise "database is locked" if a rollback was skipped
    conn.execute("ROLLBACK")
    conn.close()
    assert ledger.get(task_id=record.task_id).status == "CANCELLED"


def test_resend_of_a_failed_task_returns_it_without_reopening_it(ledger):
    record = ledger.bind("key-1", REQUEST, "cairnwork", "card-1").record
    ledger.set_status(record.task_id, "FAILED")
    again = ledger.bind("key-1", REQUEST, "cairnwork", "card-1")
    assert again.duplicate and again.record.status == "FAILED"
    with pytest.raises(InvalidStatusTransition):
        ledger.set_status(record.task_id, "DISPATCHED")
