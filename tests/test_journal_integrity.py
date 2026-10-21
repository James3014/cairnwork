"""An old journal that dispatched one task twice is named, not a raw IntegrityError."""

import sqlite3

import pytest

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
