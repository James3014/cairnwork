"""Build an in-toto Statement v1 (unsigned) from a Cairnwork notary record."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cairnwork_attest.assurance import assurance_from_record
from cairnwork_attest.record import (
    AttestError,
    VerifiedRecord,
    check_record_bindings,
)
from cairnwork_protocol import canonical_hash, canonical_json

PREDICATE_TYPE = "https://cairnwork.github.io/attestation/verified-change/v0.1"
STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
_OID = re.compile(r"^[0-9a-f]{40}$")


TEST_SOURCES = ("bundle", "auto")


class StatementInputError(AttestError):
    code: str = "STATEMENT_INPUT_ERROR"


@dataclass(frozen=True)
class Producer:
    name: str = "cairnwork"
    version: str = "0.0.1.dev0"
    git_commit: str = ""


def state_test_source(state_dir: Path) -> str:
    """``"bundle"`` only if config.json says exactly that; anything else is ``"auto"``."""
    try:
        config = json.loads((Path(state_dir) / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "auto"
    return (
        "bundle" if isinstance(config, dict) and config.get("test_source") == "bundle" else "auto"
    )


def _validate_verified_record(verified: VerifiedRecord) -> None:
    if not isinstance(verified, VerifiedRecord):
        raise StatementInputError(
            f"build_statement requires VerifiedRecord, got {type(verified).__name__}"
        )
    record = verified.record
    verdict = verified.verdict

    if not isinstance(record.candidate_commit, str) or not _OID.fullmatch(record.candidate_commit):
        raise StatementInputError(
            f"candidate_commit must be a 40-hex commit hash, got {record.candidate_commit!r}",
            code="VERIFIED_RECORD_TAMPERED",
        )
    if not isinstance(record.base_commit, str) or not _OID.fullmatch(record.base_commit):
        raise StatementInputError(
            f"base_commit must be a 40-hex commit hash, got {record.base_commit!r}",
            code="VERIFIED_RECORD_TAMPERED",
        )

    try:
        check_record_bindings(record)
    except (AttestError, AttributeError, TypeError, ValueError) as exc:
        raise StatementInputError(
            f"record bindings verification failed: {exc}",
            code="VERIFIED_RECORD_TAMPERED",
        ) from exc

    if not (
        verdict.verified
        and verdict.task_id == record.task_id
        and verdict.attempt_id == record.attempt_id
        and verdict.candidate_commit == record.candidate_commit
    ):
        raise StatementInputError(
            "verdict does not match record task_id, attempt_id, or candidate_commit, "
            "or is not verified",
            code="VERIFIED_RECORD_TAMPERED",
        )


def build_statement(
    verified: VerifiedRecord,
    *,
    producer: Producer,
    timestamp: str,
    test_source: str = "auto",
) -> dict[str, Any]:
    if test_source not in TEST_SOURCES:
        raise StatementInputError(f"test_source must be one of {TEST_SOURCES}, got {test_source!r}")
    _validate_verified_record(verified)
    record = verified.record
    assurance = assurance_from_record(verified)

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
            "test_source": test_source,
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
