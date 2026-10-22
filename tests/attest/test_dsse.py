"""Tests for cairnwork_attest.dsse (keys are generated per test; none are stored)."""

from __future__ import annotations

import base64
import copy
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from hypothesis import given, settings
from hypothesis import strategies as st

from cairnwork_attest import dsse
from cairnwork_attest.dsse import PAYLOAD_TYPE, DsseInputError, pae, sign, verify, verify_envelope
from cairnwork_attest.keys import keyid
from cairnwork_attest.statement import statement_bytes

STATEMENT = {
    "_type": "https://in-toto.io/Statement/v1",
    "subject": [{"name": "git-commit:" + "a" * 40, "digest": {"gitCommit": "a" * 40}}],
    "predicateType": "https://cairnwork.github.io/attestation/verified-change/v0.1",
    "predicate": {"status": "VERIFIED", "integrity": "VALID"},
}


@pytest.fixture
def key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _flip_payload_byte(envelope: dict[str, Any], index: int) -> dict[str, Any]:
    tampered = copy.deepcopy(envelope)
    raw = bytearray(base64.b64decode(tampered["payload"]))
    raw[index % len(raw)] ^= 0x01
    tampered["payload"] = base64.b64encode(bytes(raw)).decode("ascii")
    return tampered


def test_pae_matches_dsse_spec_example() -> None:
    # Example from the DSSE v1 protocol document.
    assert pae("http://example.com/HelloWorld", b"hello world") == (
        b"DSSEv1 29 http://example.com/HelloWorld 11 hello world"
    )


def test_sign_produces_dsse_envelope(key: Ed25519PrivateKey) -> None:
    payload = statement_bytes(STATEMENT)
    envelope = sign(payload, key)

    assert set(envelope) == {"payloadType", "payload", "signatures"}
    assert envelope["payloadType"] == PAYLOAD_TYPE == "application/vnd.in-toto+json"
    assert base64.b64decode(envelope["payload"]) == payload
    [entry] = envelope["signatures"]
    assert set(entry) == {"keyid", "sig"}
    assert entry["keyid"] == keyid(key.public_key())
    # The signature covers PAE, not the raw payload.
    key.public_key().verify(base64.b64decode(entry["sig"]), pae(PAYLOAD_TYPE, payload))


def test_sign_then_verify_passes(key: Ed25519PrivateKey) -> None:
    payload = statement_bytes(STATEMENT)
    envelope = sign(payload, key)

    assert verify(envelope, key.public_key()) is True
    result = verify_envelope(envelope, key.public_key())
    assert result.ok and result.reason == dsse.OK
    assert result.payload == payload


def test_one_flipped_payload_byte_fails(key: Ed25519PrivateKey) -> None:
    envelope = sign(statement_bytes(STATEMENT), key)
    tampered = _flip_payload_byte(envelope, 10)

    assert verify(tampered, key.public_key()) is False
    assert verify_envelope(tampered, key.public_key()).reason == dsse.BAD_SIGNATURE


def test_other_public_key_fails(key: Ed25519PrivateKey) -> None:
    envelope = sign(statement_bytes(STATEMENT), key)
    other = Ed25519PrivateKey.generate().public_key()

    assert verify(envelope, other) is False
    assert verify_envelope(envelope, other).reason == dsse.KEYID_MISMATCH


def test_other_public_key_fails_even_with_forged_keyid(key: Ed25519PrivateKey) -> None:
    envelope = sign(statement_bytes(STATEMENT), key)
    other = Ed25519PrivateKey.generate().public_key()
    envelope["signatures"][0]["keyid"] = keyid(other)

    assert verify(envelope, other) is False
    assert verify_envelope(envelope, other).reason == dsse.BAD_SIGNATURE


@pytest.mark.parametrize("signatures", [None, [], "missing"])
def test_removed_signature_fails(key: Ed25519PrivateKey, signatures: object) -> None:
    envelope = sign(statement_bytes(STATEMENT), key)
    if signatures == "missing":
        del envelope["signatures"]
    else:
        envelope["signatures"] = signatures

    assert verify(envelope, key.public_key()) is False
    assert verify_envelope(envelope, key.public_key()).reason == dsse.SIGNATURES_MISSING


def test_payload_type_is_bound_by_signature(key: Ed25519PrivateKey) -> None:
    envelope = sign(statement_bytes(STATEMENT), key)
    envelope["payloadType"] = "application/json"

    assert verify(envelope, key.public_key()) is False
    assert verify_envelope(envelope, key.public_key()).reason == dsse.PAYLOAD_TYPE_MISMATCH


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda e: "not an envelope", dsse.ENVELOPE_NOT_OBJECT),
        (lambda e: {**e, "payload": None}, dsse.PAYLOAD_MISSING),
        (lambda e: {**e, "payload": "@@not base64@@"}, dsse.PAYLOAD_NOT_BASE64),
        (lambda e: {**e, "payload": "ü"}, dsse.PAYLOAD_NOT_BASE64),
        (lambda e: {**e, "signatures": ["sig"]}, dsse.SIGNATURE_MALFORMED),
        (lambda e: {**e, "signatures": [{"keyid": None, "sig": ""}]}, dsse.SIGNATURE_MALFORMED),
        (
            lambda e: {**e, "signatures": [{**e["signatures"][0], "sig": "%%"}]},
            dsse.SIGNATURE_NOT_BASE64,
        ),
        (
            lambda e: {**e, "signatures": [{**e["signatures"][0], "sig": "AAAA"}]},
            dsse.BAD_SIGNATURE,
        ),
        (lambda e: {**e, "signatures": e["signatures"] * 17}, dsse.SIGNATURES_TOO_MANY),
    ],
)
def test_malformed_envelopes_fail_closed(key: Ed25519PrivateKey, mutate: Any, reason: str) -> None:
    envelope = mutate(sign(statement_bytes(STATEMENT), key))

    result = verify_envelope(envelope, key.public_key())
    assert result.ok is False and not result
    assert result.reason == reason
    assert result.payload is None
    assert verify(envelope, key.public_key()) is False


def test_non_ed25519_public_key_fails(key: Ed25519PrivateKey) -> None:
    envelope = sign(statement_bytes(STATEMENT), key)

    assert verify(envelope, "not a key") is False
    assert verify(envelope, key) is False  # a private key is not a trusted public key
    assert verify_envelope(envelope, None).reason == dsse.PUBLIC_KEY_INVALID


def test_extra_signature_from_other_key_does_not_hide_a_good_one(key: Ed25519PrivateKey) -> None:
    payload = statement_bytes(STATEMENT)
    envelope = sign(payload, key)
    foreign = sign(payload, Ed25519PrivateKey.generate())["signatures"][0]
    envelope["signatures"].insert(0, foreign)

    assert verify(envelope, key.public_key()) is True


def test_sign_rejects_bad_inputs(key: Ed25519PrivateKey) -> None:
    with pytest.raises(DsseInputError) as excinfo:
        sign("text", key)  # type: ignore[arg-type]
    assert excinfo.value.code == "PAYLOAD_NOT_BYTES"
    with pytest.raises(DsseInputError) as excinfo:
        sign(b"{}", key.public_key())  # type: ignore[arg-type]
    assert excinfo.value.code == "KEY_WRONG_TYPE"


_KEY = Ed25519PrivateKey.generate()  # per test session, in memory only


@settings(max_examples=200, deadline=None)
@given(payload=st.binary(min_size=1, max_size=2048), index=st.integers(min_value=0))
def test_property_roundtrip_and_any_flipped_byte(payload: bytes, index: int) -> None:
    envelope = sign(payload, _KEY)

    assert verify(envelope, _KEY.public_key()) is True
    assert verify_envelope(envelope, _KEY.public_key()).payload == payload
    assert verify(_flip_payload_byte(envelope, index), _KEY.public_key()) is False


@settings(max_examples=100, deadline=None)
@given(payload=st.binary(max_size=512), junk=st.text(max_size=64))
def test_property_garbage_signature_never_verifies(payload: bytes, junk: str) -> None:
    envelope = sign(payload, _KEY)
    envelope["signatures"][0]["sig"] = junk

    assert verify(envelope, _KEY.public_key()) is False
