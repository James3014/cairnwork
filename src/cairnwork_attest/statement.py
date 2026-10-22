"""Build an in-toto Statement v1 (unsigned) from a Cairnwork notary record."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cairnwork.notary import Verdict
from cairnwork_attest.assurance import Assurance
from cairnwork_attest.record import AttestError, RecordNotVerified, VerdictRecord
from cairnwork_protocol import canonical_hash, canonical_json

PREDICATE_TYPE = "https://cairnwork.github.io/attestation/verified-change/v0.1"
STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
_OID = re.compile(r"^[0-9a-f]{40}$")


class StatementInputError(AttestError):
    code: str = "STATEMENT_INPUT_ERROR"


@dataclass(frozen=True)
class Producer:
    name: str = "cairnwork"
    version: str = "0.0.1.dev0"
    git_commit: str = ""


def build_statement(
    record: VerdictRecord,
    *,
    verdict: Verdict,
    producer: Producer,
    assurance: Assurance,
    timestamp: str,
) -> dict[str, Any]:
    if not isinstance(record.candidate_commit, str) or not _OID.fullmatch(record.candidate_commit):
        raise StatementInputError(
            f"candidate_commit must be a 40-hex commit hash, got {record.candidate_commit!r}"
        )
    if not isinstance(record.base_commit, str) or not _OID.fullmatch(record.base_commit):
        raise StatementInputError(
            f"base_commit must be a 40-hex commit hash, got {record.base_commit!r}"
        )
    if not verdict.verified:
        reason = (
            verdict.reason or f"verdict status={verdict.status!r}, integrity={verdict.integrity!r}"
        )
        raise RecordNotVerified(reason, code="RECORD_NOT_VERIFIED")
    if (
        verdict.task_id != record.task_id
        or verdict.attempt_id != record.attempt_id
        or verdict.candidate_commit != record.candidate_commit
    ):
        raise RecordNotVerified(
            "verdict does not match record task_id, attempt_id, or candidate_commit",
            code="RECORD_NOT_VERIFIED",
        )

    core_identity: dict[str, Any] | None = None
    if isinstance(record.response, dict):
        core_identity = record.response.get("core_identity")

    verifier_observations: list[Any] = []
    if isinstance(record.request, dict):
        bundle = record.request.get("evidence_bundle")
        if isinstance(bundle, dict) and isinstance(bundle.get("observations"), list):
            verifier_observations = bundle["observations"]

    return {
        "_type": STATEMENT_TYPE,
        "subject": [
            {
                "name": f"git-commit:{record.candidate_commit}",
                "digest": {"gitCommit": record.candidate_commit},
            },
        ],
        "predicateType": PREDICATE_TYPE,
        "predicate": {
            "base_commit": record.base_commit,
            "request": record.request,
            "response": record.response,
            "request_hash": record.request_hash,
            "http_status": record.http_status,
            "status": record.status,
            "integrity": record.integrity,
            "core_identity": core_identity,
            "verifier_observations": verifier_observations,
            "hidden_test_commit": None,
            "producer": {
                "name": producer.name,
                "version": producer.version,
                "git_commit": producer.git_commit,
            },
            "recorded_at": record.recorded_at,
            "timestamp": timestamp,
            "assurance": assurance.to_dict(),
        },
    }


def statement_bytes(statement: Mapping[str, Any]) -> bytes:
    return canonical_json(statement).encode("utf-8")


def statement_hash(statement: Mapping[str, Any]) -> str:
    return canonical_hash(statement)
