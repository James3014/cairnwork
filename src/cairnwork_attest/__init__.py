"""Attestation for verified Cairnwork changes (Issue #94)."""

from __future__ import annotations

from cairnwork_attest.assurance import (
    Assurance,
    AssuranceConfigError,
    assurance_from_record,
)
from cairnwork_attest.dsse import (
    PAYLOAD_TYPE,
    DsseInputError,
    Verification,
    sign,
    verify,
    verify_envelope,
)
from cairnwork_attest.keys import (
    KeyFileError,
    generate_keypair,
    keyid,
    load_private_key,
    load_public_key_pem,
    public_key_pem,
)
from cairnwork_attest.record import (
    AttestError,
    RecordMalformed,
    RecordNotFound,
    RecordNotVerified,
    VerdictRecord,
    VerifiedRecord,
    check_record_bindings,
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
    "PAYLOAD_TYPE",
    "PREDICATE_TYPE",
    "Assurance",
    "AssuranceConfigError",
    "AttestError",
    "DsseInputError",
    "KeyFileError",
    "Producer",
    "RecordMalformed",
    "RecordNotFound",
    "RecordNotVerified",
    "StatementInputError",
    "VerdictRecord",
    "Verification",
    "VerifiedRecord",
    "assurance_from_record",
    "build_statement",
    "check_record_bindings",
    "generate_keypair",
    "keyid",
    "load_private_key",
    "load_public_key_pem",
    "public_key_pem",
    "read_record",
    "sign",
    "statement_bytes",
    "statement_hash",
    "verify",
    "verify_envelope",
    "verify_record",
]
