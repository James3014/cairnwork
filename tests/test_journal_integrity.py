"""An old journal that dispatched one task twice is named, not a raw IntegrityError."""

import sqlite3

import pytest

import cairnwork.journal as journal_module
from cairnwork.journal import Journal, JournalIntegrityViolation

OLD = (
    "CREATE TABLE attempt_journal (seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,"
    " attempt_id TEXT NOT NULL, phase TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',"
    " at TEXT NOT NULL)"
)


def test_double_dispatched_task_is_named_when_the_journal_opens(tmp_path):
    path = tmp_path / "journal.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(OLD)
    for attempt in ("a1", "a2"):
        conn.execute(
            "INSERT INTO attempt_journal (task_id, attempt_id, phase, at)"
            " VALUES ('task-x', ?, 'DISPATCHED', 'now')",
            (attempt,),
        )
    conn.commit()
    conn.close()
    with pytest.raises(JournalIntegrityViolation, match="JOURNAL_INTEGRITY_VIOLATION.*task-x"):
        Journal(path)


def test_a_clean_old_journal_gets_the_index(tmp_path):
    path = tmp_path / "journal.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(OLD)
    conn.commit()
    conn.close()
    journal = Journal(path)
    journal.record("t", "a1", "DISPATCHED")
    with pytest.raises(Exception, match="t"):
        journal.record("t", "a2", "DISPATCHED")


def _old_journal(path, tasks):
    conn = sqlite3.connect(path)
    conn.execute(OLD)
    for task in tasks:
        conn.execute(
            "INSERT INTO attempt_journal (task_id, attempt_id, phase, at)"
            " VALUES (?, 'a', 'DISPATCHED', 'now')",
            (task,),
        )
    conn.commit()
    conn.close()


def test_a_duplicate_that_slips_past_the_check_still_gives_the_named_error(tmp_path, monkeypatch):
    path = tmp_path / "journal.sqlite"
    _old_journal(path, ["task-x", "task-x"])
    monkeypatch.setattr(journal_module, "_refuse_double_dispatch", lambda conn: None)
    with pytest.raises(JournalIntegrityViolation, match="JOURNAL_INTEGRITY_VIOLATION"):
        Journal(path)


def test_no_writer_can_insert_between_the_check_and_the_index(tmp_path, monkeypatch):
    path = tmp_path / "journal.sqlite"
    _old_journal(path, ["task-x"])
    real, outcome = journal_module._refuse_double_dispatch, []

    def check_then_try_a_concurrent_insert(conn):
        real(conn)
        other = sqlite3.connect(path, timeout=0.2)
        try:
            other.execute(
                "INSERT INTO attempt_journal (task_id, attempt_id, phase, at)"
                " VALUES ('task-x', 'b', 'DISPATCHED', 'now')"
            )
            outcome.append("inserted")
        except sqlite3.OperationalError:
            outcome.append("locked")
        finally:
            other.close()

    monkeypatch.setattr(
        journal_module, "_refuse_double_dispatch", check_then_try_a_concurrent_insert
    )
    Journal(path)
    assert outcome == ["locked"]
