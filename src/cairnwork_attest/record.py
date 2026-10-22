"""Read-only access to Cairnwork notary verification records."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cairnwork.notary import Notary, NotaryInputError, NotarySchemaMismatch, Verdict

_OID = re.compile(r"^[0-9a-f]{40}$")
_VERIFIED_TOKEN = object()


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


class RecordNotVerified(AttestError):
    code: str = "RECORD_NOT_VERIFIED"


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    res: dict[str, Any] = {}
    for key, value in pairs:
        if key in res:
            raise RecordMalformed(f"duplicate key in JSON: {key!r}", code="DUPLICATE_KEY")
        res[key] = value
    return res


def _reject_constant(val: str) -> Any:
    raise RecordMalformed(f"unsupported JSON constant: {val!r}", code="RECORD_MALFORMED")


def _parse_json_dict(raw: str, prefix: str) -> dict[str, Any]:
    try:
        parsed = json.loads(
            raw,
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_constant,
        )
    except RecordMalformed:
        raise
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


def _dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_dumps(value).encode("utf-8")).hexdigest()


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
        if self.request_json:
            parsed_req = _parse_json_dict(self.request_json, "request")
            if not self.request:
                object.__setattr__(self, "request", parsed_req)
        if self.response_json:
            parsed_resp = _parse_json_dict(self.response_json, "response")
            if not self.response:
                object.__setattr__(self, "response", parsed_resp)


@dataclass(frozen=True)
class VerifiedRecord:
    record: VerdictRecord
    verdict: Verdict
    _token: object = field(default=None, repr=False, compare=False, kw_only=True)

    def __new__(cls, *args: Any, **kwargs: Any) -> VerifiedRecord:
        if kwargs.get("_token") is not _VERIFIED_TOKEN:
            raise AttestError(
                "VerifiedRecord cannot be constructed directly",
                code="VERIFIED_RECORD_FORGED",
            )
        return super().__new__(cls)

    def __post_init__(self) -> None:
        if self._token is not _VERIFIED_TOKEN:
            raise AttestError(
                "VerifiedRecord cannot be constructed directly",
                code="VERIFIED_RECORD_FORGED",
            )


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
    )


def _check_record_bindings(record: VerdictRecord) -> None:
    expected_hash = _digest(record.request)
    if record.request_hash != expected_hash:
        raise RecordMalformed(
            f"request_hash {record.request_hash!r} != recomputed {expected_hash!r}",
            code="RECORD_MALFORMED",
        )
    change_set = record.request.get("change_set")
    if not isinstance(change_set, dict):
        raise RecordMalformed("request missing change_set object", code="RECORD_MALFORMED")
    target_rev = change_set.get("target_revision")
    source_rev = change_set.get("source_revision")
    if not isinstance(target_rev, str) or not isinstance(source_rev, str):
        raise RecordMalformed("change_set revisions must be strings", code="RECORD_MALFORMED")
    req_candidate = target_rev.removeprefix("git-commit:")
    req_base = source_rev.removeprefix("git-commit:")
    if record.candidate_commit != req_candidate:
        raise RecordMalformed(
            f"candidate_commit {record.candidate_commit!r} != target_revision {req_candidate!r}",
            code="RECORD_MALFORMED",
        )
    if record.base_commit != req_base:
        raise RecordMalformed(
            f"base_commit {record.base_commit!r} != source_revision {req_base!r}",
            code="RECORD_MALFORMED",
        )


def _never_call_core(_: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    raise RuntimeError("attest never calls Core")


def verify_record(
    notary_sqlite: Path,
    task_id: str,
    attempt_id: str,
    *,
    expected_candidate: str,
    core_identity: dict[str, Any] | None = None,
) -> VerifiedRecord:
    if not isinstance(expected_candidate, str) or not _OID.fullmatch(expected_candidate):
        raise RecordMalformed(
            "expected_candidate must be a 40-hex commit id", code="RECORD_MALFORMED"
        )
    record = read_record(notary_sqlite, task_id, attempt_id)
    if record.candidate_commit != expected_candidate:
        raise RecordNotVerified(
            f"candidate_commit {record.candidate_commit!r}"
            f" != expected_candidate {expected_candidate!r}",
            code="CANDIDATE_MISMATCH",
        )
    _check_record_bindings(record)

    try:
        notary = Notary(notary_sqlite, core_verify=_never_call_core, core_identity=core_identity)
        verdict = notary.verdict(task_id, attempt_id, expected_candidate=expected_candidate)
    except (NotaryInputError, NotarySchemaMismatch) as exc:
        raise RecordMalformed(str(exc), code="RECORD_MALFORMED") from exc

    if verdict is None or not verdict.verified:
        reason = (
            verdict.reason
            if verdict and verdict.reason
            else (
                f"verdict not verified (status={verdict.status if verdict else 'NONE'}, "
                f"integrity={verdict.integrity if verdict else 'NONE'}, "
                f"http_status={verdict.http_status if verdict else 0})"
            )
        )
        raise RecordNotVerified(reason, code="RECORD_NOT_VERIFIED")

    return VerifiedRecord(record=record, verdict=verdict, _token=_VERIFIED_TOKEN)
