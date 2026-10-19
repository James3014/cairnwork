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

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cairnwork.changeset import read_change_set
from cairnwork.core_request import AcceptanceSpec, VerifierObservation, build_core_request

CoreVerify = Callable[[dict[str, Any]], tuple[int, dict[str, Any]]]
CORE_ERROR = "CORE_ERROR"
SCHEMA_VERSION = 1
_OID = re.compile(r"^[0-9a-f]{40}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS core_verifications (
    seq              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id          TEXT NOT NULL,
    attempt_id       TEXT NOT NULL,
    base_commit      TEXT NOT NULL,
    candidate_commit TEXT NOT NULL,
    request_hash     TEXT NOT NULL,
    request_json     TEXT NOT NULL,
    http_status      INTEGER NOT NULL,
    status           TEXT NOT NULL,
    integrity        TEXT NOT NULL,
    response_json    TEXT NOT NULL,
    recorded_at      TEXT NOT NULL,
    UNIQUE (task_id, attempt_id)
);
CREATE TRIGGER IF NOT EXISTS core_verifications_no_update
    BEFORE UPDATE ON core_verifications
    BEGIN SELECT RAISE(ABORT, 'core_verifications is append-only'); END;
CREATE TRIGGER IF NOT EXISTS core_verifications_no_delete
    BEFORE DELETE ON core_verifications
    BEGIN SELECT RAISE(ABORT, 'core_verifications is append-only'); END;
CREATE TRIGGER IF NOT EXISTS core_verifications_no_overwrite
    BEFORE INSERT ON core_verifications
    WHEN EXISTS (SELECT 1 FROM core_verifications
                 WHERE task_id = NEW.task_id AND attempt_id = NEW.attempt_id)
    BEGIN SELECT RAISE(ABORT, 'core_verifications is append-only'); END;
"""

_HASH_KEYS = (
    "acceptance_contract_hash",
    "change_set_hash",
    "verification_plan_hash",
    "evidence_bundle_hash",
)


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


def default_core_verify() -> CoreVerify:
    from product.adapters.generic_verification import verify_generic_changeset

    return verify_generic_changeset


class Notary:
    def __init__(self, path: Path, core_verify: CoreVerify | None = None) -> None:
        self._path = Path(path)
        self._core_verify = core_verify or default_core_verify()
        conn = self._connect()
        try:
            conn.execute("BEGIN")  # one read snapshot for both checks
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'core_verifications'"
            ).fetchone()
            conn.execute("COMMIT")
            if has_table and version != SCHEMA_VERSION:
                raise NotarySchemaMismatch(f"user_version {version}, need {SCHEMA_VERSION}")
            if not has_table:  # only a new database is written to; readers never take the lock
                # Tables, triggers and user_version commit together, so no other connection
                # can see the tables without the version (or race to a half-built schema).
                conn.executescript(
                    f"BEGIN IMMEDIATE;{_SCHEMA}PRAGMA user_version = {SCHEMA_VERSION};COMMIT;"
                )
        finally:
            conn.close()

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
        )
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO core_verifications (task_id, attempt_id, base_commit,"
                " candidate_commit, request_hash, request_json, http_status, status,"
                " integrity, response_json, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
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
        if not isinstance(expected_candidate, str) or not _OID.match(expected_candidate):
            raise NotaryInputError("expected_candidate must be a 40-hex commit id")
        return self._read(task_id, attempt_id, expected_candidate)

    def _read(
        self, task_id: str, attempt_id: str, expected_candidate: str | None
    ) -> Verdict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT base_commit, candidate_commit, request_hash, request_json, http_status,"
                " status, integrity, response_json FROM core_verifications"
                " WHERE task_id = ? AND attempt_id = ?",
                (task_id, attempt_id),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        base, candidate, request_hash, request_json, http_status, status, integrity, resp = row
        request, response = json.loads(request_json), json.loads(resp)
        verified = _is_verified(http_status, status, integrity, response, request, request_hash)
        verified = verified and _columns_match(request, task_id, attempt_id, base, candidate)
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


def _status_and_integrity(response: dict[str, Any]) -> tuple[str, str]:
    verification = response.get("verification")
    if isinstance(verification, dict):
        return str(verification.get("status")), str(verification.get("integrity"))
    error = response.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    return str(code or CORE_ERROR), "UNKNOWN"


def _is_verified(
    http_status: int,
    status: str,
    integrity: str,
    response: dict[str, Any],
    request: dict[str, Any],
    request_hash: str,
) -> bool:
    if http_status != 200 or status != "VERIFIED" or integrity != "VALID":
        return False
    if _digest(request) != request_hash:
        return False
    echoed = response.get("hashes")
    expected = _request_hashes(request)
    if not isinstance(echoed, dict):
        return False
    return all(expected[key] is not None and echoed.get(key) == expected[key] for key in _HASH_KEYS)


def _columns_match(
    request: dict[str, Any], task_id: str, attempt_id: str, base: str, candidate: str
) -> bool:
    """A row's key and commit columns must be the ones its stored request was built for."""
    change_set, contract = request.get("change_set"), request.get("acceptance_contract")
    if not isinstance(change_set, dict) or not isinstance(contract, dict):
        return False
    return (
        contract.get("contract_id") == task_id
        and change_set.get("change_set_id") == f"{task_id}/{attempt_id}"
        and change_set.get("source_revision") == f"git-commit:{base}"
        and change_set.get("target_revision") == f"git-commit:{candidate}"
    )


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


def _dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _frozen(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(_dumps(value))


def _digest(value: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_dumps(value).encode("utf-8")).hexdigest()
