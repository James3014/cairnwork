"""Core notarisation with an append-only record (preserved behaviors #16-#18).

Only a nexus-certify response with HTTP 200, status VERIFIED, integrity VALID
and hashes that match the request Cairnwork built counts as verified. Every
other outcome, including an exception while calling Core, is recorded and is
not verified.

The notary builds the Core request itself from the task's acceptance spec, the
git change set and Cairnwork's verifier observations, so a verdict is always
bound to one exact change. It reads the change set from git itself (callers
pass a repository and two commit ids, never a change set), and the stored row
keeps the full request, the base and candidate commits, and its digest. Every
reader must name the candidate commit it holds: ``verdict(..., expected_candidate=)``
is required, and a row whose columns disagree with its stored request is not
verified.

The record is append-only: one row per (task_id, attempt_id), enforced by a
UNIQUE constraint and by triggers that abort UPDATE, DELETE and any INSERT
(including INSERT OR REPLACE from any connection) for an attempt already recorded. The
first outcome of an attempt is final. Calling ``notarize`` again for the same
attempt with the same request returns it without calling Core; with a
different request it raises AttemptRequestMismatch. Changed evidence needs a
new attempt. This avoids Nexus-new's INSERT OR REPLACE, where a later VERIFIED
could hide an earlier FAILED_VERIFICATION (Nexus-new#2776).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cairnwork.changeset import GitChangeSet, read_change_set
from cairnwork.core_request import AcceptanceSpec, VerifierObservation, build_core_request
from cairnwork.notary_schema import SCHEMA, SCHEMA_VERSION
from cairnwork.notary_verify import (  # readback helpers, split out for the 400-line cap
    CORE_ERROR,
    _columns_match,
    _digest,
    _dumps,
    _frozen,
    _is_verified,
    _status_and_integrity,
)
from cairnwork.receipt_binding import request_matches
from cairnwork_protocol import (
    IDENTITY_MISMATCH,
    canonical_json,
    response_binding_problem,
)

CoreVerify = Callable[[dict[str, Any]], tuple[int, dict[str, Any]]]
_OID = re.compile(r"^[0-9a-f]{40}$")


class NotaryError(Exception):
    code = "NOTARY_ERROR"

    def __init__(self, detail: str = "") -> None:
        super().__init__(f"{self.code}: {detail}" if detail else self.code)


class AttemptRequestMismatch(NotaryError):
    code = "ATTEMPT_REQUEST_MISMATCH"


class NotaryInputError(NotaryError):
    code = "NOTARY_INPUT_ERROR"


class NotarySchemaMismatch(NotaryError):
    code = "NOTARY_SCHEMA_MISMATCH"


@dataclass(frozen=True)
class Verdict:
    task_id: str
    attempt_id: str
    base_commit: str
    candidate_commit: str
    request_hash: str
    http_status: int
    status: str
    integrity: str
    verified: bool
    response: dict[str, Any]
    reason: str = ""  # set when Core said VERIFIED but the response is not bound to the request


def default_core_verify() -> CoreVerify:
    from product.adapters.generic_verification import verify_generic_changeset

    return verify_generic_changeset


def default_core_identity() -> dict[str, Any]:
    from product.protocol import core_identity

    return core_identity()


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Add the expected_identity column and bump to schema 2 (Issue #74); existing append-only rows
    keep a NULL value. Serialised by BEGIN IMMEDIATE and idempotent under a concurrent migration."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:  # another connection migrated while we waited for the lock
            conn.execute("COMMIT")
            return
        if version != 1:
            raise NotarySchemaMismatch(f"user_version {version}, need {SCHEMA_VERSION}")
        conn.execute("ALTER TABLE core_verifications ADD COLUMN expected_identity TEXT")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


class Notary:
    def __init__(
        self,
        path: Path,
        core_verify: CoreVerify | None = None,
        core_identity: dict[str, Any] | None = None,
    ) -> None:
        """core_identity is the pin a Core response must report as itself; None expects the Core
        installed beside Cairnwork. Kept separate from the resolved default so a readback can tell a
        real operator pin from the local default (Issue #74)."""
        self._path = Path(path)
        self._core_verify = core_verify or default_core_verify()
        self._core_identity = core_identity  # the configured pin, or None
        self._default: dict[str, Any] | None = None  # the local Core identity, resolved once
        conn = self._connect()
        try:
            conn.execute("BEGIN")  # one read snapshot for both checks
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'core_verifications'"
            ).fetchone()
            conn.execute("COMMIT")
            if not has_table:  # only a new database is written to; readers never take the lock
                # Tables, triggers and user_version commit together, so no other connection
                # can see the tables without the version (or race to a half-built schema).
                conn.executescript(
                    f"BEGIN IMMEDIATE;{SCHEMA}PRAGMA user_version = {SCHEMA_VERSION};COMMIT;"
                )
            elif version == 1 and SCHEMA_VERSION == 2:
                _migrate_v1_to_v2(conn)
            elif version != SCHEMA_VERSION:
                raise NotarySchemaMismatch(f"user_version {version}, need {SCHEMA_VERSION}")
        finally:
            conn.close()

    def _expected_identity(self) -> dict[str, Any]:
        """Expected identity with no per-row value: the pin, else the local Core (cached)."""
        if self._core_identity is not None:
            return self._core_identity
        if self._default is None:
            self._default = default_core_identity()
        return self._default

    def _identity_problem(
        self, request: dict[str, Any], response: dict[str, Any], expected_identity: str | None
    ) -> str:
        """Why the response's Core identity is not acceptable, else "". A schema-2 row is checked
        against the identity it was notarized against, so it survives a Core upgrade (#74); a pin
        must also equal that. A pre-migration (NULL) row keeps the read-time identity."""
        if expected_identity is None:
            return response_binding_problem(request, response, self._expected_identity()) or ""
        expected = json.loads(expected_identity)
        reason = response_binding_problem(request, response, expected) or ""
        if reason:
            return reason
        pin = self._core_identity
        if pin is not None and canonical_json(expected) != canonical_json(pin):
            return IDENTITY_MISMATCH
        return ""

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def notarize(
        self,
        task_id: str,
        attempt_id: str,
        spec: AcceptanceSpec,
        repo: Path,
        base_commit: str,
        candidate_commit: str,
        observations: tuple[VerifierObservation, ...],
    ) -> Verdict:
        """Verify one attempt with Core, once; later calls return the recorded verdict."""
        if spec.contract_id != task_id:
            raise NotaryInputError(f"spec.contract_id {spec.contract_id!r} is not task {task_id!r}")
        change = read_change_set(repo, base_commit, candidate_commit)
        request = _frozen(build_core_request(spec, change, observations, attempt_id=attempt_id))
        request_hash = _digest(request)
        recorded = self._recorded(task_id, attempt_id, request_hash)
        if recorded is not None:
            return recorded
        http_status, response = self._call_core(_frozen(request))
        status, integrity = _status_and_integrity(response)
        row = (
            task_id,
            attempt_id,
            change.base_commit,
            change.candidate_commit,
            request_hash,
            _dumps(request),
            http_status,
            status,
            integrity,
            _dumps(response),
            datetime.now(UTC).isoformat(),
            _dumps(self._expected_identity()),  # frozen so readback survives a later Core upgrade
        )
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO core_verifications (task_id, attempt_id, base_commit,"
                " candidate_commit, request_hash, request_json, http_status, status,"
                " integrity, response_json, recorded_at, expected_identity)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
        except sqlite3.IntegrityError:
            pass  # a concurrent call recorded this attempt first; its outcome is final
        finally:
            conn.close()
        stored = self._recorded(task_id, attempt_id, request_hash)
        if stored is None:  # pragma: no cover - only if the row vanished
            raise NotaryError("core verification was not recorded")
        return stored

    def verdict(self, task_id: str, attempt_id: str, *, expected_candidate: str) -> Verdict | None:
        """Read the recorded verdict; it is unverified unless it is for expected_candidate."""
        if not isinstance(expected_candidate, str) or not _OID.fullmatch(expected_candidate):
            raise NotaryInputError("expected_candidate must be a 40-hex commit id")
        return self._read(task_id, attempt_id, expected_candidate)

    def bound_to(self, task_id: str, attempt_id: str, change: GitChangeSet) -> bool:
        """Attempt is VERIFIED and its stored request is this git change (#61); fails closed."""
        try:
            verdict = self._read(task_id, attempt_id, change.candidate_commit)
            if verdict is None or not verdict.verified:
                return False
            conn = self._connect()
            try:
                sql = "SELECT request_json FROM core_verifications"
                row = conn.execute(sql + " WHERE task_id = ? AND attempt_id = ?",
                                   (task_id, attempt_id)).fetchone()  # fmt: skip
            finally:
                conn.close()
        except sqlite3.Error:
            return False
        return row is not None and request_matches(json.loads(row[0]), change)

    def _read(
        self, task_id: str, attempt_id: str, expected_candidate: str | None
    ) -> Verdict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT base_commit, candidate_commit, request_hash, request_json, http_status,"
                " status, integrity, response_json, expected_identity FROM core_verifications"
                " WHERE task_id = ? AND attempt_id = ?",
                (task_id, attempt_id),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        base, candidate, request_hash, request_json, http_status, status, integrity, resp, exp = row
        request, response = json.loads(request_json), json.loads(resp)
        verified = _is_verified(http_status, status, integrity, response, request, request_hash)
        reason = ""
        if verified:
            reason = self._identity_problem(request, response, exp)
        verified = (
            verified
            and not reason
            and _columns_match(request, task_id, attempt_id, base, candidate)
        )
        if expected_candidate is not None and expected_candidate != candidate:
            verified = False
        return Verdict(
            task_id,
            attempt_id,
            base,
            candidate,
            request_hash,
            http_status,
            status,
            integrity,
            verified,
            response,
            reason,
        )

    def _recorded(self, task_id: str, attempt_id: str, request_hash: str) -> Verdict | None:
        recorded = self._read(task_id, attempt_id, expected_candidate=None)
        if recorded is not None and recorded.request_hash != request_hash:
            raise AttemptRequestMismatch(
                f"{task_id}/{attempt_id} was notarized for another request"
            )
        return recorded

    def _call_core(self, request: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        try:
            http_status, response = self._core_verify(request)
        except Exception as exc:  # noqa: BLE001 - any Core failure is recorded as not verified
            return 0, {"error": {"code": CORE_ERROR, "type": type(exc).__name__}}
        if type(http_status) is not int or not isinstance(response, dict):
            return 0, {"error": {"code": CORE_ERROR, "type": "MALFORMED_CORE_RESPONSE"}}
        try:
            return http_status, _frozen(response)
        except (TypeError, ValueError):
            return 0, {"error": {"code": CORE_ERROR, "type": "UNSERIALISABLE_CORE_RESPONSE"}}
