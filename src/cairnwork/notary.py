"""Core notarisation with an append-only record (preserved behaviors #16-#18).

Only a nexus-certify response with HTTP 200, status VERIFIED, integrity VALID
and hashes that match the request Cairnwork sent counts as verified. Every
other outcome, including an exception while calling Core, is recorded and is
not verified.

The record is append-only: one row per (task_id, attempt_id), enforced by a
UNIQUE constraint and by triggers that abort any UPDATE or DELETE. The first
recorded outcome of an attempt is final; a later call for the same attempt
returns it without calling Core again. Changed evidence needs a new attempt.
This avoids Nexus-new's INSERT OR REPLACE, where a later VERIFIED could hide an
earlier FAILED_VERIFICATION (Nexus-new#2776).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CoreVerify = Callable[[dict[str, Any]], tuple[int, dict[str, Any]]]
CORE_ERROR = "CORE_ERROR"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS core_verifications (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT NOT NULL,
    attempt_id    TEXT NOT NULL,
    request_hash  TEXT NOT NULL,
    http_status   INTEGER NOT NULL,
    status        TEXT NOT NULL,
    integrity     TEXT NOT NULL,
    response_json TEXT NOT NULL,
    recorded_at   TEXT NOT NULL,
    UNIQUE (task_id, attempt_id)
);
CREATE TRIGGER IF NOT EXISTS core_verifications_no_update
    BEFORE UPDATE ON core_verifications
    BEGIN SELECT RAISE(ABORT, 'core_verifications is append-only'); END;
CREATE TRIGGER IF NOT EXISTS core_verifications_no_delete
    BEFORE DELETE ON core_verifications
    BEGIN SELECT RAISE(ABORT, 'core_verifications is append-only'); END;
"""

_HASH_KEYS = (
    "acceptance_contract_hash",
    "change_set_hash",
    "verification_plan_hash",
    "evidence_bundle_hash",
)


@dataclass(frozen=True)
class Verdict:
    task_id: str
    attempt_id: str
    http_status: int
    status: str
    integrity: str
    verified: bool
    response: dict[str, Any]


def default_core_verify() -> CoreVerify:
    from product.adapters.generic_verification import verify_generic_changeset

    return verify_generic_changeset


class Notary:
    def __init__(self, path: Path, core_verify: CoreVerify | None = None) -> None:
        self._path = Path(path)
        self._core_verify = core_verify or default_core_verify()
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA recursive_triggers=ON")  # REPLACE's implicit delete hits the trigger
        return conn

    def notarize(self, task_id: str, attempt_id: str, request: dict[str, Any]) -> Verdict:
        """Verify one attempt with Core, once; later calls return the recorded verdict."""
        recorded = self.verdict(task_id, attempt_id)
        if recorded is not None:
            return recorded
        request_hash = _digest(request)
        http_status, response = self._call_core(request)
        status, integrity = _status_and_integrity(response)
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO core_verifications"
                " (task_id, attempt_id, request_hash, http_status, status, integrity,"
                " response_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    attempt_id,
                    request_hash,
                    http_status,
                    status,
                    integrity,
                    json.dumps(response, sort_keys=True),
                    datetime.now(UTC).isoformat(),
                ),
            )
        finally:
            conn.close()
        stored = self.verdict(task_id, attempt_id)
        if stored is None:
            raise RuntimeError("core verification was not recorded")
        return stored

    def verdict(self, task_id: str, attempt_id: str) -> Verdict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT request_hash, http_status, status, integrity, response_json"
                " FROM core_verifications WHERE task_id = ? AND attempt_id = ?",
                (task_id, attempt_id),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        request_hash, http_status, status, integrity, response_json = row
        response = json.loads(response_json)
        verified = _is_verified(http_status, status, integrity, response, request_hash)
        return Verdict(task_id, attempt_id, http_status, status, integrity, verified, response)

    def _call_core(self, request: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        try:
            http_status, response = self._core_verify(request)
        except Exception as exc:  # noqa: BLE001 - any Core failure is recorded as not verified
            return 0, {"error": {"code": CORE_ERROR, "type": type(exc).__name__}}
        if not isinstance(http_status, int) or not isinstance(response, dict):
            return 0, {"error": {"code": CORE_ERROR, "type": "MALFORMED_CORE_RESPONSE"}}
        return http_status, {**response, "_request_hashes": _request_hashes(request)}


def _status_and_integrity(response: dict[str, Any]) -> tuple[str, str]:
    verification = response.get("verification")
    if not isinstance(verification, dict):
        code = (
            (response.get("error") or {}).get("code")
            if isinstance(response.get("error"), dict)
            else None
        )
        return str(code or CORE_ERROR), "UNKNOWN"
    return str(verification.get("status")), str(verification.get("integrity"))


def _is_verified(
    http_status: int, status: str, integrity: str, response: dict[str, Any], request_hash: str
) -> bool:
    if http_status != 200 or status != "VERIFIED" or integrity != "VALID":
        return False
    echoed = response.get("hashes")
    sent = response.get("_request_hashes")
    if not isinstance(echoed, dict) or not isinstance(sent, dict) or not request_hash:
        return False
    return all(sent.get(key) is not None and echoed.get(key) == sent.get(key) for key in _HASH_KEYS)


def _request_hashes(request: dict[str, Any]) -> dict[str, str | None]:
    from product.protocol.generic_verification import (
        acceptance_contract_hash,
        change_set_hash,
        evidence_bundle_hash,
        verification_plan_hash,
    )

    try:
        return {
            "acceptance_contract_hash": acceptance_contract_hash(request["acceptance_contract"]),
            "change_set_hash": change_set_hash(request["change_set"]),
            "verification_plan_hash": verification_plan_hash(request["verification_plan"]),
            "evidence_bundle_hash": evidence_bundle_hash(request["evidence_bundle"]),
        }
    except (KeyError, TypeError, ValueError):
        return dict.fromkeys(_HASH_KEYS)


def _digest(value: dict[str, Any]) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
