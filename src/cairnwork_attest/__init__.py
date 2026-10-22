"""Attestation for verified Cairnwork changes (Issue #94)."""

from __future__ import annotations

from cairnwork_attest.assurance import (
    Assurance,
    AssuranceConfigError,
    assurance_from_record,
)
from cairnwork_attest.record import (
    AttestError,
    RecordMalformed,
    RecordNotFound,
    RecordNotVerified,
    VerdictRecord,
    VerifiedRecord,
    read_record,
    verify_record,
)
from cairnwork_attest.statement import (
    PREDICATE_TYPE,
    Producer,
    StatementInputError,
    build_statement,
    statement_bytes,
    statement_hash,
)

__all__ = [
    "PREDICATE_TYPE",
    "Assurance",
    "AssuranceConfigError",
    "AttestError",
    "Producer",
    "RecordMalformed",
    "RecordNotFound",
    "RecordNotVerified",
    "StatementInputError",
    "VerdictRecord",
    "VerifiedRecord",
    "assurance_from_record",
    "build_statement",
    "read_record",
    "statement_bytes",
    "statement_hash",
    "verify_record",
]
