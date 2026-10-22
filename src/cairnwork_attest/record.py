"""Read-only access to Cairnwork notary verification records."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class AttestError(Exception):
    code: str = "ATTEST_ERROR"

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        self.code = code or self.__class__.code
        self.message = message
        detail = f"{self.code}: {message}" if message else self.code
        super().__init__(detail)


class RecordNotFound(AttestError):
    code: str = "RECORD_NOT_FOUND"


class RecordMalformed(AttestError):
    code: str = "RECORD_MALFORMED"


def _parse_json_dict(raw: str, prefix: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RecordMalformed(
            f"{prefix}_json is not valid JSON: {exc}",
            code=f"INVALID_{prefix.upper()}_JSON",
        ) from exc
    if not isinstance(parsed, dict):
        raise RecordMalformed(
            f"{prefix}_json must decode to a JSON object",
            code=f"INVALID_{prefix.upper()}_STRUCTURE",
        )
    return parsed


@dataclass(frozen=True)
class VerdictRecord:
    task_id: str
    attempt_id: str
    base_commit: str
    candidate_commit: str
    request_hash: str
    request_json: str
    http_status: int
    status: str
    integrity: str
    response_json: str
    recorded_at: str
    request: dict[str, Any] = field(default_factory=dict)
    response: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.request and self.request_json:
            object.__setattr__(self, "request", _parse_json_dict(self.request_json, "request"))
        if not self.response and self.response_json:
            object.__setattr__(self, "response", _parse_json_dict(self.response_json, "response"))


def _fetch_row(uri: str, task_id: str, attempt_id: str) -> tuple[Any, ...]:
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=30)
    except sqlite3.Error as exc:
        raise RecordNotFound(f"failed to open database: {exc}", code="RECORD_NOT_FOUND") from exc

    try:
        query = (
            "SELECT task_id, attempt_id, base_commit, candidate_commit, request_hash,"
            " request_json, http_status, status, integrity, response_json, recorded_at"
            " FROM core_verifications WHERE task_id = ? AND attempt_id = ?"
        )
        try:
            row = conn.execute(query, (task_id, attempt_id)).fetchone()
        except sqlite3.Error as exc:
            raise RecordNotFound(f"query failed: {exc}", code="RECORD_NOT_FOUND") from exc
    finally:
        conn.close()

    if row is None:
        raise RecordNotFound(
            f"record not found for task_id={task_id!r}, attempt_id={attempt_id!r}",
            code="RECORD_NOT_FOUND",
        )
    return row


def read_record(notary_sqlite: Path, task_id: str, attempt_id: str) -> VerdictRecord:
    path = Path(notary_sqlite).resolve()
    if not path.is_file():
        raise RecordNotFound(f"notary database does not exist: {path}", code="RECORD_NOT_FOUND")

    row = _fetch_row(f"file:{path.as_posix()}?mode=ro", task_id, attempt_id)
    try:
        (
            row_task_id,
            row_attempt_id,
            base_commit,
            candidate_commit,
            request_hash,
            request_json,
            http_status,
            status,
            integrity,
            response_json,
            recorded_at,
        ) = row
    except (TypeError, ValueError) as exc:
        raise RecordMalformed(f"malformed database row: {exc}", code="RECORD_MALFORMED") from exc

    return VerdictRecord(
        task_id=row_task_id,
        attempt_id=row_attempt_id,
        base_commit=base_commit,
        candidate_commit=candidate_commit,
        request_hash=request_hash,
        request_json=request_json,
        http_status=http_status,
        status=status,
        integrity=integrity,
        response_json=response_json,
        recorded_at=recorded_at,
        request=_parse_json_dict(request_json, "request"),
        response=_parse_json_dict(response_json, "response"),
    )
