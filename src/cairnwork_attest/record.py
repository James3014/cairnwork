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
from cairnwork.notary_verify import _columns_match

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


def parse_strict_json(
    raw: str,
    *,
    error_cls: type[AttestError] = RecordMalformed,
    malformed_code: str = "RECORD_MALFORMED",
    constant_code: str = "RECORD_MALFORMED",
) -> Any:
    """Parse JSON strictly: reject duplicate keys, NaN, Infinity, and -Infinity."""

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        res: dict[str, Any] = {}
        for key, value in pairs:
            if key in res:
                raise error_cls(f"duplicate key in JSON: {key!r}", code="DUPLICATE_KEY")
            res[key] = value
        return res

    def reject_constant(val: str) -> Any:
        raise error_cls(f"unsupported JSON constant: {val!r}", code=constant_code)

    try:
        return json.loads(
            raw,
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except AttestError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
        raise error_cls(f"invalid JSON: {exc}", code=malformed_code) from exc


def _parse_json_dict(raw: str, prefix: str) -> dict[str, Any]:
    parsed = parse_strict_json(
        raw,
        error_cls=RecordMalformed,
        malformed_code=f"INVALID_{prefix.upper()}_JSON",
        constant_code="RECORD_MALFORMED",
    )
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
    binding_json: str | None = None  # #229: an external candidate's binding (schema 3)
    binding: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.binding_json is not None and self.binding is None:
            object.__setattr__(self, "binding", _parse_json_dict(self.binding_json, "binding"))
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
        object.__setattr__(self, "_token", None)


def _fetch_row(uri: str, task_id: str, attempt_id: str) -> tuple[Any, ...]:
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=30)
    except sqlite3.Error as exc:
        raise RecordNotFound(f"failed to open database: {exc}", code="RECORD_NOT_FOUND") from exc

    try:
        try:  # a schema-2 ledger (not yet migrated) has no binding_json: read it as NULL
            columns = {r[1] for r in conn.execute("PRAGMA table_info(core_verifications)")}
            binding = "binding_json" if "binding_json" in columns else "NULL"
            query = (
                "SELECT task_id, attempt_id, base_commit, candidate_commit, request_hash,"
                " request_json, http_status, status, integrity, response_json, recorded_at,"
                f" {binding} FROM core_verifications WHERE task_id = ? AND attempt_id = ?"
            )
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
            binding_json,
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
        binding_json=binding_json,
    )


def check_record_bindings(record: VerdictRecord) -> None:
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
    if record.binding is not None:  # #229: the target is the executed tree the binding names
        if not _columns_match(
            record.request, record.task_id, record.attempt_id, record.base_commit,
            record.candidate_commit, record.binding,
        ):  # fmt: skip
            raise RecordMalformed("binding is not the one the request was built for",
                                  code="BINDING_HASH_MISMATCH")  # fmt: skip
        return
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


_check_record_bindings = check_record_bindings


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
    check_record_bindings(record)

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
