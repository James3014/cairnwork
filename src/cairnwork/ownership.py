"""Durable task ownership ledger (preserved behaviors #6 and #7).

Every task is bound to exactly one owning system before dispatch. The binding
is one atomic SQLite transaction keyed by idempotency key, and it carries the
request digest, the owner and the task id. Resend, query, cancel and recovery
route by this record; when no record exists the caller must stop, never guess.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

OWNERS = frozenset({"cairnwork", "nexus-new"})
INACTIVE_STATUSES = frozenset({"SUPERSEDED", "CANCELLED"})
STATUSES = frozenset({"BOUND", "DISPATCHED", "COMPLETED", "FAILED"}) | INACTIVE_STATUSES

_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_ownership (
    idempotency_key TEXT PRIMARY KEY,
    request_digest  TEXT NOT NULL,
    owner           TEXT NOT NULL,
    task_id         TEXT NOT NULL UNIQUE,
    card_hash       TEXT NOT NULL,
    status          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS task_ownership_card ON task_ownership (card_hash);
"""


class OwnershipError(Exception):
    """Base class; every subclass carries a stable machine-readable code."""

    code = "OWNERSHIP_ERROR"

    def __init__(self, detail: str = "") -> None:
        super().__init__(f"{self.code}: {detail}" if detail else self.code)


class KeyReusedWithDifferentRequest(OwnershipError):
    code = "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_REQUEST"


class DuplicateLogicalTask(OwnershipError):
    code = "DUPLICATE_LOGICAL_TASK"


class OwnershipUnknown(OwnershipError):
    code = "OWNERSHIP_UNKNOWN"


class InvalidOwnershipValue(OwnershipError):
    code = "INVALID_OWNERSHIP_VALUE"


@dataclass(frozen=True)
class OwnershipRecord:
    idempotency_key: str
    request_digest: str
    owner: str
    task_id: str
    card_hash: str
    status: str


@dataclass(frozen=True)
class BindResult:
    record: OwnershipRecord
    duplicate: bool


def request_digest(request: dict[str, Any]) -> str:
    """sha256 over canonical JSON, so key order and whitespace do not matter."""
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class OwnershipLedger:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def bind(
        self, idempotency_key: str, request: dict[str, Any], owner: str, card_hash: str
    ) -> BindResult:
        """Bind a new task, or return the existing binding for an identical resend."""
        _require(idempotency_key, "idempotency_key")
        _require(card_hash, "card_hash")
        if owner not in OWNERS:
            raise InvalidOwnershipValue(f"owner {owner!r}")
        digest = request_digest(request)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = _select_by_key(conn, idempotency_key)
            if existing is not None:
                conn.execute("ROLLBACK")
                if existing.request_digest != digest:
                    raise KeyReusedWithDifferentRequest(idempotency_key)
                return BindResult(existing, duplicate=True)
            active = _active_for_card(conn, card_hash)
            if active is not None:
                conn.execute("ROLLBACK")
                raise DuplicateLogicalTask(f"card {card_hash} owned by task {active.task_id}")
            record = OwnershipRecord(
                idempotency_key, digest, owner, f"task-{uuid.uuid4().hex}", card_hash, "BOUND"
            )
            conn.execute(
                "INSERT INTO task_ownership VALUES (?, ?, ?, ?, ?, ?)",
                (
                    record.idempotency_key,
                    record.request_digest,
                    record.owner,
                    record.task_id,
                    record.card_hash,
                    record.status,
                ),
            )
            conn.execute("COMMIT")
            return BindResult(record, duplicate=False)
        finally:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            conn.close()

    def route(self, *, task_id: str | None = None, idempotency_key: str | None = None) -> str:
        """Return the owning system; raise OwnershipUnknown instead of guessing."""
        return self.get(task_id=task_id, idempotency_key=idempotency_key).owner

    def get(
        self, *, task_id: str | None = None, idempotency_key: str | None = None
    ) -> OwnershipRecord:
        if (task_id is None) == (idempotency_key is None):
            raise InvalidOwnershipValue("give exactly one of task_id or idempotency_key")
        conn = self._connect()
        try:
            if task_id is not None:
                row = conn.execute(
                    "SELECT * FROM task_ownership WHERE task_id = ?", (task_id,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM task_ownership WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise OwnershipUnknown(task_id or idempotency_key or "")
        return OwnershipRecord(*row)

    def set_status(self, task_id: str, status: str) -> None:
        """Change the status only; the owner of a task can never change."""
        if status not in STATUSES:
            raise InvalidOwnershipValue(f"status {status!r}")
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE task_ownership SET status = ? WHERE task_id = ?", (status, task_id)
            )
        finally:
            conn.close()
        if cur.rowcount != 1:
            raise OwnershipUnknown(task_id)


def _require(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InvalidOwnershipValue(f"{name} must be a non-empty string")


def _select_by_key(conn: sqlite3.Connection, key: str) -> OwnershipRecord | None:
    row = conn.execute("SELECT * FROM task_ownership WHERE idempotency_key = ?", (key,)).fetchone()
    return OwnershipRecord(*row) if row else None


def _active_for_card(conn: sqlite3.Connection, card_hash: str) -> OwnershipRecord | None:
    placeholders = ",".join("?" for _ in INACTIVE_STATUSES)
    row = conn.execute(
        f"SELECT * FROM task_ownership WHERE card_hash = ? AND status NOT IN ({placeholders})",  # noqa: S608
        (card_hash, *sorted(INACTIVE_STATUSES)),
    ).fetchone()
    return OwnershipRecord(*row) if row else None
