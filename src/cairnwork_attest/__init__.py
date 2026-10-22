"""Attestation for verified Cairnwork changes (Issue #94)."""

from __future__ import annotations

from cairnwork_attest.assurance import (
    Assurance,
    AssuranceConfigError,
    assurance_from_record,
)
from cairnwork_attest.bundle import (
    BundleError,
    read_bundle,
    write_bundle,
)
from cairnwork_attest.dsse import (
    PAYLOAD_TYPE,
    DsseInputError,
    Verification,
    sign_statement,
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
    parse_strict_json,
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
from cairnwork_attest.verify import (
    VerifyResult,
    verify_bundle,
)

__all__ = [
    "PAYLOAD_TYPE",
    "PREDICATE_TYPE",
    "Assurance",
    "AssuranceConfigError",
    "AttestError",
    "BundleError",
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
    "VerifyResult",
    "assurance_from_record",
    "build_statement",
    "check_record_bindings",
    "generate_keypair",
    "keyid",
    "load_private_key",
    "load_public_key_pem",
    "parse_strict_json",
    "public_key_pem",
    "read_bundle",
    "read_record",
    "sign_statement",
    "statement_bytes",
    "statement_hash",
    "verify",
    "verify_bundle",
    "verify_envelope",
    "verify_record",
    "write_bundle",
]
