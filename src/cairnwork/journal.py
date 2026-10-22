"""Append-only attempt journal: what a run was about to do, written before it does it.

Recovery (recovery.py) reads the last phase of each attempt to decide what a crash
left behind. Rows are never updated or deleted (triggers), so a crash can only
lose the step it was in, never rewrite history.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

PHASES = (
    "DISPATCHED",  # attempt id chosen, ledger moved to DISPATCHED
    "WORKER_STARTED",  # clone created, worker about to run
    "CANDIDATE",  # candidate commit created and checked (detail = commit id)
    "TEST_SOURCE",  # hidden-test mode this run used (detail = bundle | auto); read by attest
    "VERIFYING",  # verifiers about to run on the candidate
    "NOTARIZING",  # Core about to be called
    "FINISHED",  # ledger status about to be written (detail = COMPLETED or FAILED)
)

_TABLE = """
CREATE TABLE IF NOT EXISTS attempt_journal (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    phase      TEXT NOT NULL CHECK (phase IN
        ('DISPATCHED', 'WORKER_STARTED', 'CANDIDATE', 'TEST_SOURCE', 'VERIFYING', 'NOTARIZING',
         'FINISHED')),
    detail     TEXT NOT NULL DEFAULT '',
    at         TEXT NOT NULL
)
"""

_SCHEMA = (
    _TABLE
    + ";"
    + """
CREATE INDEX IF NOT EXISTS attempt_journal_task ON attempt_journal (task_id);
CREATE TRIGGER IF NOT EXISTS attempt_journal_no_update BEFORE UPDATE ON attempt_journal
    BEGIN SELECT RAISE(ABORT, 'attempt_journal is append-only'); END;
CREATE TRIGGER IF NOT EXISTS attempt_journal_no_delete BEFORE DELETE ON attempt_journal
    BEGIN SELECT RAISE(ABORT, 'attempt_journal is append-only'); END;
"""
)

_ONE_DISPATCH = (
    "CREATE UNIQUE INDEX IF NOT EXISTS attempt_journal_one_dispatch"
    " ON attempt_journal (task_id) WHERE phase = 'DISPATCHED'"
)


class JournalIntegrityViolation(Exception):
    """The journal already has two DISPATCHED rows for one task; reconcile it by hand."""

    code = "JOURNAL_INTEGRITY_VIOLATION"


class AlreadyDispatched(Exception):
    """A DISPATCHED row already exists for this task: only one attempt may start."""


@dataclass(frozen=True)
class Entry:
    task_id: str
    attempt_id: str
    phase: str
    detail: str


class Journal:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        conn = self._connect()
        try:
            _widen_phase_check(conn)
            conn.executescript(_SCHEMA)
            conn.execute("BEGIN IMMEDIATE")  # the check and the index share one write lock
            try:
                _refuse_double_dispatch(conn)
                conn.execute(_ONE_DISPATCH)
                conn.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                raise JournalIntegrityViolation(f"{JournalIntegrityViolation.code}: {exc}") from exc
            finally:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def record(self, task_id: str, attempt_id: str, phase: str, detail: str = "") -> None:
        if phase not in PHASES:
            raise ValueError(f"unknown phase {phase!r}")
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO attempt_journal (task_id, attempt_id, phase, detail, at)"
                " VALUES (?, ?, ?, ?, ?)",
                (task_id, attempt_id, phase, detail, datetime.now(UTC).isoformat()),
            )
        except sqlite3.IntegrityError as exc:
            if phase == "DISPATCHED":
                raise AlreadyDispatched(task_id) from exc
            raise
        finally:
            conn.close()

    def entries(self, task_id: str) -> list[Entry]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT task_id, attempt_id, phase, detail FROM attempt_journal"
                " WHERE task_id = ? ORDER BY seq",
                (task_id,),
            ).fetchall()
        finally:
            conn.close()
        return [Entry(*row) for row in rows]


def _widen_phase_check(conn: sqlite3.Connection) -> None:
    """A journal created before TEST_SOURCE existed has the old CHECK: rebuild it, keeping rows."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'attempt_journal'"
    ).fetchone()
    if row is None or "'TEST_SOURCE'" in row[0]:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("ALTER TABLE attempt_journal RENAME TO attempt_journal_old")
        conn.execute("DROP TRIGGER IF EXISTS attempt_journal_no_update")
        conn.execute("DROP TRIGGER IF EXISTS attempt_journal_no_delete")
        conn.execute("DROP INDEX IF EXISTS attempt_journal_task")
        conn.execute("DROP INDEX IF EXISTS attempt_journal_one_dispatch")
        conn.execute(_TABLE)
        conn.execute(
            "INSERT INTO attempt_journal (seq, task_id, attempt_id, phase, detail, at)"
            " SELECT seq, task_id, attempt_id, phase, detail, at FROM attempt_journal_old"
        )
        conn.execute("DROP TABLE attempt_journal_old")
        conn.execute("COMMIT")
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")


def _refuse_double_dispatch(conn: sqlite3.Connection) -> None:
    """Name the tasks an old journal dispatched twice, before the unique index is built."""
    rows = conn.execute(
        "SELECT task_id FROM attempt_journal WHERE phase = 'DISPATCHED'"
        " GROUP BY task_id HAVING count(*) > 1 ORDER BY task_id"
    ).fetchall()
    if rows:
        tasks = ", ".join(r[0] for r in rows)
        raise JournalIntegrityViolation(
            f"{JournalIntegrityViolation.code}: dispatched twice: {tasks}"
        )
