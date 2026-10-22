"""Tests for cairnwork_attest.dsse (keys are generated per test; none are stored)."""

from __future__ import annotations

import base64
import copy
from pathlib import Path
from typing import Any

import pytest
from conftest import commit_change, git
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from hypothesis import given, settings
from hypothesis import strategies as st

from cairnwork.core_request import AcceptanceSpec, VerifierObservation
from cairnwork.notary import Notary, default_core_identity
from cairnwork_attest import dsse
from cairnwork_attest.dsse import (
    PAYLOAD_TYPE,
    DsseInputError,
    _sign_bytes,
    pae,
    sign_statement,
    verify,
    verify_envelope,
)
from cairnwork_attest.keys import keyid
from cairnwork_attest.record import VerifiedRecord, verify_record
from cairnwork_attest.statement import (
    Producer,
    StatementInputError,
    build_statement,
    statement_bytes,
)
from cairnwork_protocol import (
    acceptance_contract_hash,
    change_manifest_hash,
    change_set_hash,
    evidence_bundle_hash,
    verification_plan_hash,
)

CORE_IDENTITY = default_core_identity()
PRODUCER = Producer(name="cairnwork", version="0.0.1.dev0", git_commit="c" * 40)
TIMESTAMP = "2026-10-22T09:00:00Z"


def fake_core_verify(request: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    response = {
        "protocol_version": request.get("protocol_version", "0.1.0-experimental"),
        "verification": {
            "status": "VERIFIED",
            "integrity": "VALID",
            "reason_codes": [],
        },
        "hashes": {
            "acceptance_contract_hash": acceptance_contract_hash(request["acceptance_contract"]),
            "change_set_hash": change_set_hash(request["change_set"]),
            "verification_plan_hash": verification_plan_hash(request["verification_plan"]),
            "evidence_bundle_hash": evidence_bundle_hash(request["evidence_bundle"]),
            "change_manifest_hash": change_manifest_hash(request["change_manifest"]),
        },
        "core_identity": CORE_IDENTITY,
    }
    return 200, response


def setup_sample_notary(
    sqlite_path: Path,
    repo: Path,
    task_id: str = "task-1",
    attempt_id: str = "a1",
) -> tuple[Notary, Any]:
    notary = Notary(sqlite_path, core_verify=fake_core_verify, core_identity=CORE_IDENTITY)
    base_commit = git(repo, "rev-parse", "HEAD")
    candidate_commit = commit_change(repo, {"src/a.py": "x = 2\n"})
    spec = AcceptanceSpec(task_id, "sha256:" + "c" * 64, ("pytest",), ("src/a.py",), False)
    obs = (VerifierObservation("pytest", "artifact-1", "sha256:" + "e" * 64, True),)
    verdict = notary.notarize(task_id, attempt_id, spec, repo, base_commit, candidate_commit, obs)
    return notary, verdict


@pytest.fixture
def verified(tmp_path: Path, repo: Path) -> VerifiedRecord:
    db_path = tmp_path / "notary.sqlite"
    _, verdict = setup_sample_notary(db_path, repo, "task-1", "a1")
    return verify_record(db_path, "task-1", "a1", expected_candidate=verdict.candidate_commit)


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
    assert pae("http://example.com/HelloWorld", b"hello world") == (
        b"DSSEv1 29 http://example.com/HelloWorld 11 hello world"
    )


def test_sign_statement_produces_dsse_envelope(
    verified: VerifiedRecord, key: Ed25519PrivateKey
) -> None:
    envelope = sign_statement(verified, producer=PRODUCER, timestamp=TIMESTAMP, private_key=key)
    stmt = build_statement(verified, producer=PRODUCER, timestamp=TIMESTAMP)
    expected_payload = statement_bytes(stmt)

    assert set(envelope) == {"payloadType", "payload", "signatures"}
    assert envelope["payloadType"] == PAYLOAD_TYPE == "application/vnd.in-toto+json"
    assert base64.b64decode(envelope["payload"]) == expected_payload
    [entry] = envelope["signatures"]
    assert set(entry) == {"keyid", "sig"}
    assert entry["keyid"] == keyid(key.public_key())
    key.public_key().verify(base64.b64decode(entry["sig"]), pae(PAYLOAD_TYPE, expected_payload))


def test_sign_then_verify_passes(verified: VerifiedRecord, key: Ed25519PrivateKey) -> None:
    envelope = sign_statement(verified, producer=PRODUCER, timestamp=TIMESTAMP, private_key=key)
    stmt = build_statement(verified, producer=PRODUCER, timestamp=TIMESTAMP)
    expected_payload = statement_bytes(stmt)

    assert verify(envelope, key.public_key()) is True
    result = verify_envelope(envelope, key.public_key())
    assert result.ok and result.reason == dsse.OK
    assert result.payload == expected_payload
    assert result.signature_index == 0


def test_one_flipped_payload_byte_fails(verified: VerifiedRecord, key: Ed25519PrivateKey) -> None:
    envelope = sign_statement(verified, producer=PRODUCER, timestamp=TIMESTAMP, private_key=key)
    tampered = _flip_payload_byte(envelope, 10)

    assert verify(tampered, key.public_key()) is False
    assert verify_envelope(tampered, key.public_key()).reason == dsse.BAD_SIGNATURE


def test_other_public_key_fails(verified: VerifiedRecord, key: Ed25519PrivateKey) -> None:
    envelope = sign_statement(verified, producer=PRODUCER, timestamp=TIMESTAMP, private_key=key)
    other = Ed25519PrivateKey.generate().public_key()

    assert verify(envelope, other) is False
    assert verify_envelope(envelope, other).reason == dsse.KEYID_MISMATCH


def test_other_public_key_fails_even_with_forged_keyid(
    verified: VerifiedRecord, key: Ed25519PrivateKey
) -> None:
    envelope = sign_statement(verified, producer=PRODUCER, timestamp=TIMESTAMP, private_key=key)
    other = Ed25519PrivateKey.generate().public_key()
    envelope["signatures"][0]["keyid"] = keyid(other)

    assert verify(envelope, other) is False
    assert verify_envelope(envelope, other).reason == dsse.BAD_SIGNATURE


@pytest.mark.parametrize("signatures", [None, [], "missing"])
def test_removed_signature_fails(
    verified: VerifiedRecord, key: Ed25519PrivateKey, signatures: object
) -> None:
    envelope = sign_statement(verified, producer=PRODUCER, timestamp=TIMESTAMP, private_key=key)
    if signatures == "missing":
        del envelope["signatures"]
    else:
        envelope["signatures"] = signatures

    assert verify(envelope, key.public_key()) is False
    assert verify_envelope(envelope, key.public_key()).reason == dsse.SIGNATURES_MISSING


def test_payload_type_is_bound_by_signature(
    verified: VerifiedRecord, key: Ed25519PrivateKey
) -> None:
    envelope = sign_statement(verified, producer=PRODUCER, timestamp=TIMESTAMP, private_key=key)
    envelope["payloadType"] = "application/json"

    assert verify(envelope, key.public_key()) is False
    assert verify_envelope(envelope, key.public_key()).reason == dsse.PAYLOAD_TYPE_MISMATCH


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda e: "not an envelope", dsse.ENVELOPE_NOT_OBJECT),
        (lambda e: {**e, "extra_top_level": "forbidden"}, dsse.ENVELOPE_SHAPE),
        (lambda e: {**e, "payload": None}, dsse.PAYLOAD_MISSING),
        (lambda e: {**e, "payload": "@@not base64@@"}, dsse.PAYLOAD_NOT_BASE64),
        (lambda e: {**e, "payload": "ü"}, dsse.PAYLOAD_NOT_BASE64),
        (lambda e: {**e, "payload": "-_--"}, dsse.PAYLOAD_NOT_BASE64),
        (lambda e: {**e, "payload": "ZE=="}, dsse.PAYLOAD_NOT_BASE64),
        (
            lambda e: {**e, "signatures": [{"keyid": e["signatures"][0]["keyid"], "sig": None}]},
            dsse.SIGNATURE_MALFORMED,
        ),
        (
            lambda e: {**e, "signatures": [{**e["signatures"][0], "sig": "%%"}]},
            dsse.SIGNATURE_NOT_BASE64,
        ),
        (
            lambda e: {**e, "signatures": [{**e["signatures"][0], "sig": "-_--"}]},
            dsse.SIGNATURE_NOT_BASE64,
        ),
        (
            lambda e: {**e, "signatures": [{**e["signatures"][0], "sig": "-_=="}]},
            dsse.SIGNATURE_NOT_BASE64,
        ),
        (
            lambda e: {**e, "signatures": [{**e["signatures"][0], "sig": "ZE=="}]},
            dsse.SIGNATURE_NOT_BASE64,
        ),
        (
            lambda e: {**e, "signatures": [{**e["signatures"][0], "sig": "AAAA"}]},
            dsse.BAD_SIGNATURE,
        ),
        (lambda e: {**e, "signatures": e["signatures"] * 17}, dsse.SIGNATURES_TOO_MANY),
    ],
)
def test_malformed_envelopes_fail_closed(
    verified: VerifiedRecord, key: Ed25519PrivateKey, mutate: Any, reason: str
) -> None:
    envelope = mutate(
        sign_statement(verified, producer=PRODUCER, timestamp=TIMESTAMP, private_key=key)
    )

    result = verify_envelope(envelope, key.public_key())
    assert result.ok is False and not result
    assert result.reason == reason
    assert result.payload is None
    assert result.signature_index is None
    assert verify(envelope, key.public_key()) is False


def test_non_ed25519_public_key_fails(verified: VerifiedRecord, key: Ed25519PrivateKey) -> None:
    envelope = sign_statement(verified, producer=PRODUCER, timestamp=TIMESTAMP, private_key=key)

    assert verify(envelope, "not a key") is False
    assert verify(envelope, key) is False
    assert verify_envelope(envelope, None).reason == dsse.PUBLIC_KEY_INVALID


def test_multi_signature_semantics(verified: VerifiedRecord, key: Ed25519PrivateKey) -> None:
    envelope = sign_statement(verified, producer=PRODUCER, timestamp=TIMESTAMP, private_key=key)
    trusted_sig = envelope["signatures"][0]

    stale_other = {"keyid": "deadbeef" * 8, "sig": base64.b64encode(b"s" * 64).decode("ascii")}
    malformed_other_key = {"keyid": "cafebabe" * 8, "sig": 12345}
    malformed_entry = "not even a dict"
    invalid_b64_other = {"keyid": "11223344" * 8, "sig": "not base64!"}

    envelope["signatures"] = [
        stale_other,
        malformed_other_key,
        trusted_sig,
        malformed_entry,
        invalid_b64_other,
    ]

    result = verify_envelope(envelope, key.public_key())
    assert result.ok is True
    assert result.reason == dsse.OK
    assert result.signature_index == 2
    assert result.payload is not None

    trusted_kid = keyid(key.public_key())
    malformed_trusted = {"keyid": trusted_kid, "sig": "@@not base64@@"}
    envelope["signatures"].append(malformed_trusted)

    bad_result = verify_envelope(envelope, key.public_key())
    assert bad_result.ok is False
    assert bad_result.reason == dsse.SIGNATURE_NOT_BASE64

    envelope["signatures"][-1] = {"keyid": trusted_kid, "sig": None}
    bad_result2 = verify_envelope(envelope, key.public_key())
    assert bad_result2.ok is False
    assert bad_result2.reason == dsse.SIGNATURE_MALFORMED


def test_sign_statement_rejects_bad_inputs(
    verified: VerifiedRecord, key: Ed25519PrivateKey
) -> None:
    bad_verified: Any = "not a verified record"
    with pytest.raises(StatementInputError):
        sign_statement(bad_verified, producer=PRODUCER, timestamp=TIMESTAMP, private_key=key)

    bad_key: Any = key.public_key()
    with pytest.raises(DsseInputError) as excinfo:
        sign_statement(verified, producer=PRODUCER, timestamp=TIMESTAMP, private_key=bad_key)
    assert excinfo.value.code == "KEY_WRONG_TYPE"


def test_sign_bytes_rejects_bad_inputs(key: Ed25519PrivateKey) -> None:
    bad_payload: Any = "text"
    with pytest.raises(DsseInputError) as excinfo:
        _sign_bytes(bad_payload, key)
    assert excinfo.value.code == "PAYLOAD_NOT_BYTES"

    bad_key: Any = key.public_key()
    with pytest.raises(DsseInputError) as excinfo:
        _sign_bytes(b"{}", bad_key)
    assert excinfo.value.code == "KEY_WRONG_TYPE"


_KEY = Ed25519PrivateKey.generate()


@settings(max_examples=200, deadline=None)
@given(payload=st.binary(min_size=1, max_size=2048), index=st.integers(min_value=0))
def test_property_roundtrip_and_any_flipped_byte(payload: bytes, index: int) -> None:
    envelope = _sign_bytes(payload, _KEY)

    assert verify(envelope, _KEY.public_key()) is True
    res = verify_envelope(envelope, _KEY.public_key())
    assert res.payload == payload
    assert res.signature_index == 0
    assert verify(_flip_payload_byte(envelope, index), _KEY.public_key()) is False


@settings(max_examples=100, deadline=None)
@given(payload=st.binary(max_size=512), junk=st.text(max_size=64))
def test_property_garbage_signature_never_verifies(payload: bytes, junk: str) -> None:
    envelope = _sign_bytes(payload, _KEY)
    envelope["signatures"][0]["sig"] = junk

    assert verify(envelope, _KEY.public_key()) is False
