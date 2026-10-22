"""DSSE v1 envelopes over in-toto Statements, signed with Ed25519.

https://github.com/secure-systems-lab/dsse/blob/master/protocol.md: the signature
covers PAE(payloadType, payload), never the base64 text or the JSON envelope.

`verify` is fail-closed: anything it does not positively check (wrong shape, wrong
payloadType, bad base64, unknown key, bad signature) is False, with a reason code
available from `verify_envelope`.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from cairnwork_attest.keys import keyid
from cairnwork_attest.record import AttestError, VerifiedRecord
from cairnwork_attest.statement import Producer, build_statement, statement_bytes

PAYLOAD_TYPE = "application/vnd.in-toto+json"
ED25519_SIGNATURE_LEN = 64
MAX_SIGNATURES = 16

# Reason codes returned by verify_envelope.
OK = "OK"
ENVELOPE_NOT_OBJECT = "ENVELOPE_NOT_OBJECT"
PAYLOAD_TYPE_MISMATCH = "PAYLOAD_TYPE_MISMATCH"
PAYLOAD_MISSING = "PAYLOAD_MISSING"
PAYLOAD_NOT_BASE64 = "PAYLOAD_NOT_BASE64"
SIGNATURES_MISSING = "SIGNATURES_MISSING"
SIGNATURES_TOO_MANY = "SIGNATURES_TOO_MANY"
SIGNATURE_MALFORMED = "SIGNATURE_MALFORMED"
SIGNATURE_NOT_BASE64 = "SIGNATURE_NOT_BASE64"
KEYID_MISMATCH = "KEYID_MISMATCH"
BAD_SIGNATURE = "BAD_SIGNATURE"
PUBLIC_KEY_INVALID = "PUBLIC_KEY_INVALID"

_STRICT_B64_RE = re.compile(r"^[A-Za-z0-9+/]*={0,2}$")


class DsseInputError(AttestError):
    code: str = "DSSE_INPUT_ERROR"


@dataclass(frozen=True)
class Verification:
    ok: bool
    reason: str
    payload: bytes | None = None  # set only when ok: the authenticated statement bytes
    signature_index: int | None = None  # which entry in signatures verified

    def __bool__(self) -> bool:
        return self.ok


def pae(payload_type: str, payload: bytes) -> bytes:
    """DSSE v1 Pre-Authentication Encoding."""
    type_bytes = payload_type.encode("utf-8")
    return b"DSSEv1 %d %b %d %b" % (len(type_bytes), type_bytes, len(payload), payload)


def _strict_b64decode(original: str) -> bytes | None:
    if not isinstance(original, str) or not _STRICT_B64_RE.fullmatch(original):
        return None
    try:
        raw_ascii = original.encode("ascii")
        decoded = base64.b64decode(raw_ascii, validate=True)
        if base64.b64encode(decoded) != raw_ascii:
            return None
        return decoded
    except (binascii.Error, UnicodeEncodeError, ValueError):
        return None


def _sign_bytes(statement_bytes: bytes, private_key: Ed25519PrivateKey) -> dict[str, Any]:
    """Return a DSSE envelope whose single signature covers PAE(in-toto, statement)."""
    if not isinstance(statement_bytes, bytes):
        raise DsseInputError("statement_bytes must be bytes", code="PAYLOAD_NOT_BYTES")
    if not isinstance(private_key, Ed25519PrivateKey):
        raise DsseInputError("private_key must be an Ed25519 private key", code="KEY_WRONG_TYPE")
    signature = private_key.sign(pae(PAYLOAD_TYPE, statement_bytes))
    return {
        "payloadType": PAYLOAD_TYPE,
        "payload": base64.b64encode(statement_bytes).decode("ascii"),
        "signatures": [
            {
                "keyid": keyid(private_key.public_key()),
                "sig": base64.b64encode(signature).decode("ascii"),
            }
        ],
    }


def sign_statement(
    verified: VerifiedRecord,
    *,
    producer: Producer,
    timestamp: str,
    private_key: Ed25519PrivateKey,
) -> dict[str, Any]:
    """Build a Statement from a verified record and return its signed DSSE envelope."""
    statement = build_statement(verified, producer=producer, timestamp=timestamp)
    payload = statement_bytes(statement)
    return _sign_bytes(payload, private_key)


def _fail(reason: str) -> Verification:
    return Verification(ok=False, reason=reason)


def verify_envelope(envelope: object, public_key: object) -> Verification:
    """Check `envelope` against one trusted Ed25519 public key.

    Passes only if some signature whose keyid names this key verifies over
    PAE(payloadType, payload). Every other outcome is a failure with a reason code.
    """
    if not isinstance(public_key, Ed25519PublicKey):
        return _fail(PUBLIC_KEY_INVALID)
    if not isinstance(envelope, Mapping):
        return _fail(ENVELOPE_NOT_OBJECT)
    if envelope.get("payloadType") != PAYLOAD_TYPE:
        return _fail(PAYLOAD_TYPE_MISMATCH)
    payload_b64 = envelope.get("payload")
    if not isinstance(payload_b64, str):
        return _fail(PAYLOAD_MISSING)
    payload = _strict_b64decode(payload_b64)
    if payload is None:
        return _fail(PAYLOAD_NOT_BASE64)
    signatures = envelope.get("signatures")
    if not isinstance(signatures, list) or not signatures:
        return _fail(SIGNATURES_MISSING)
    if len(signatures) > MAX_SIGNATURES:
        return _fail(SIGNATURES_TOO_MANY)

    expected_keyid = keyid(public_key)
    message = pae(PAYLOAD_TYPE, payload)

    # A malformed entry under the TRUSTED keyid must fail closed.
    for entry in signatures:
        if isinstance(entry, Mapping) and entry.get("keyid") == expected_keyid:
            sig_b64 = entry.get("sig")
            if not isinstance(sig_b64, str):
                return _fail(SIGNATURE_MALFORMED)
            if _strict_b64decode(sig_b64) is None:
                return _fail(SIGNATURE_NOT_BASE64)

    reason = KEYID_MISMATCH
    for idx, entry in enumerate(signatures):
        if not isinstance(entry, Mapping) or entry.get("keyid") != expected_keyid:
            continue
        sig_b64 = entry["sig"]
        sig = _strict_b64decode(sig_b64)
        if sig is None:
            return _fail(SIGNATURE_NOT_BASE64)
        if len(sig) != ED25519_SIGNATURE_LEN:
            reason = BAD_SIGNATURE
            continue
        try:
            public_key.verify(sig, message)
        except InvalidSignature:
            reason = BAD_SIGNATURE
            continue
        return Verification(ok=True, reason=OK, payload=payload, signature_index=idx)
    return _fail(reason)


def verify(envelope: object, public_key: object) -> bool:
    """True only when `verify_envelope` positively verified the envelope."""
    return verify_envelope(envelope, public_key).ok is True
