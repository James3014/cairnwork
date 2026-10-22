"""Tests for cairnwork_attest.bundle and cairnwork_attest.verify."""

from __future__ import annotations

import base64
import copy
import socket
from pathlib import Path
from typing import Any

import pytest
from conftest import commit_change, git

from cairnwork.core_request import AcceptanceSpec, VerifierObservation
from cairnwork.notary import Notary, default_core_identity
from cairnwork_attest.bundle import BundleError, read_bundle, write_bundle
from cairnwork_attest.dsse import _sign_bytes, sign_statement
from cairnwork_attest.keys import (
    generate_keypair,
    load_private_key,
    public_key_pem,
)
from cairnwork_attest.record import _digest, verify_record
from cairnwork_attest.statement import (
    Producer,
    statement_bytes,
)
from cairnwork_attest.verify import verify_bundle
from cairnwork_protocol import (
    acceptance_contract_hash,
    change_manifest_hash,
    change_set_hash,
    evidence_bundle_hash,
    verification_plan_hash,
)

CORE_IDENTITY = default_core_identity()


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


def setup_verified_bundle_fixture(
    state_dir: Path,
    repo: Path,
    *,
    task_id: str = "task-1",
    attempt_id: str = "a1",
) -> tuple[Path, Path, str, str]:
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / "notary.sqlite"
    notary = Notary(db_path, core_verify=fake_core_verify, core_identity=CORE_IDENTITY)
    base_commit = git(repo, "rev-parse", "HEAD")
    candidate_commit = commit_change(repo, {"src/a.py": "x = 2\n"})
    spec = AcceptanceSpec(task_id, "sha256:" + "c" * 64, ("pytest",), ("src/a.py",), False)
    obs = (VerifierObservation("pytest", "artifact-1", "sha256:" + "e" * 64, True),)
    notary.notarize(task_id, attempt_id, spec, repo, base_commit, candidate_commit, obs)

    pubkey = generate_keypair(state_dir)
    pub_pem_path = state_dir / "public.pem"
    pub_pem_path.write_text(public_key_pem(pubkey), encoding="ascii")

    verified = verify_record(db_path, task_id, attempt_id, expected_candidate=candidate_commit)
    privkey = load_private_key(state_dir)
    envelope = sign_statement(
        verified,
        producer=Producer(name="cairnwork", version="0.0.1.dev0", git_commit=""),
        timestamp="2026-10-22T10:00:00Z",
        private_key=privkey,
    )
    bundle_path = state_dir / "bundle.json"
    write_bundle(bundle_path, envelope)

    return bundle_path, pub_pem_path, base_commit, candidate_commit


def test_bundle_write_and_read_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "bundle.json"
    sample = {"payloadType": "application/vnd.in-toto+json", "payload": "dGVzdA=="}
    write_bundle(path, sample)
    assert path.is_file()
    read_back = read_bundle(path)
    assert read_back == sample


def test_write_bundle_refuses_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "bundle.json"
    sample = {"a": 1}
    write_bundle(path, sample)
    with pytest.raises(BundleError) as exc_info:
        write_bundle(path, sample)
    assert exc_info.value.code == "BUNDLE_EXISTS"


def test_read_bundle_missing_and_size_cap(tmp_path: Path) -> None:
    missing = tmp_path / "nonexistent.json"
    with pytest.raises(BundleError) as exc:
        read_bundle(missing)
    assert exc.value.code == "BUNDLE_NOT_FOUND"

    large = tmp_path / "large.json"
    large.write_bytes(b" " * (4 * 1024 * 1024 + 1))
    with pytest.raises(BundleError) as exc:
        read_bundle(large)
    assert exc.value.code == "BUNDLE_TOO_LARGE"


@pytest.mark.parametrize(
    ("content", "expected_code"),
    [
        ('{"a": 1, "a": 2}', "DUPLICATE_KEY"),
        ('{"a": NaN}', "BUNDLE_MALFORMED"),
        ('{"a": Infinity}', "BUNDLE_MALFORMED"),
        ("invalid-json{", "BUNDLE_MALFORMED"),
        ("[1, 2, 3]", "BUNDLE_MALFORMED"),
    ],
)
def test_read_bundle_strict_json(tmp_path: Path, content: str, expected_code: str) -> None:
    p = tmp_path / "test.json"
    p.write_text(content, encoding="utf-8")
    with pytest.raises(BundleError) as exc:
        read_bundle(p)
    assert exc.value.code == expected_code


def test_verify_bundle_happy_path(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_socket(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("network socket forbidden")

    monkeypatch.setattr(socket, "socket", fail_socket)

    bundle, pub_pem, _, candidate = setup_verified_bundle_fixture(tmp_path / "state", repo)
    result = verify_bundle(bundle, public_key_pem=pub_pem, repo=repo)

    assert result.ok is True
    assert result.code == "OK"
    assert result.detail == ""
    assert result.statement is not None
    assert result.statement["subject"][0]["digest"]["gitCommit"] == candidate
    assert bool(result) is True


def test_verify_bundle_signature_invalid_tamper_payload(tmp_path: Path, repo: Path) -> None:
    bundle, pub_pem, _, _ = setup_verified_bundle_fixture(tmp_path / "state", repo)
    envelope = read_bundle(bundle)
    raw_payload = base64.b64decode(envelope["payload"])
    tampered_bytes = raw_payload[:-1] + (b"X" if raw_payload[-1:] != b"X" else b"Y")
    envelope["payload"] = base64.b64encode(tampered_bytes).decode("ascii")

    tampered_path = tmp_path / "tampered_bundle.json"
    write_bundle(tampered_path, envelope)

    result = verify_bundle(tampered_path, public_key_pem=pub_pem, repo=repo)
    assert result.ok is False
    assert result.code == "SIGNATURE_INVALID"


def test_verify_bundle_signature_invalid_wrong_public_key(tmp_path: Path, repo: Path) -> None:
    bundle, _, _, _ = setup_verified_bundle_fixture(tmp_path / "state", repo)
    other_state = tmp_path / "other_state"
    other_state.mkdir()
    other_pub = generate_keypair(other_state)
    other_pem_path = other_state / "other_pub.pem"
    other_pem_path.write_text(public_key_pem(other_pub), encoding="ascii")

    result = verify_bundle(bundle, public_key_pem=other_pem_path, repo=repo)
    assert result.ok is False
    assert result.code == "SIGNATURE_INVALID"


def _resign_statement(state_dir: Path, stmt: dict[str, Any], out_path: Path) -> Path:
    privkey = load_private_key(state_dir)
    env = _sign_bytes(statement_bytes(stmt), privkey)
    write_bundle(out_path, env)
    return out_path


def test_verify_bundle_statement_shape(tmp_path: Path, repo: Path) -> None:
    state_dir = tmp_path / "state"
    bundle, pub_pem, _, _ = setup_verified_bundle_fixture(state_dir, repo)
    res = verify_bundle(bundle, public_key_pem=pub_pem, repo=repo)
    assert res.statement is not None

    stmt = copy.deepcopy(res.statement)
    stmt["_type"] = "https://invalid.type/v1"
    tampered_path = _resign_statement(state_dir, stmt, tmp_path / "b1.json")
    result = verify_bundle(tampered_path, public_key_pem=pub_pem, repo=repo)
    assert result.ok is False
    assert result.code == "STATEMENT_SHAPE"

    stmt2 = copy.deepcopy(res.statement)
    stmt2["predicateType"] = "https://invalid.predicate/v1"
    tampered_path2 = _resign_statement(state_dir, stmt2, tmp_path / "b2.json")
    result2 = verify_bundle(tampered_path2, public_key_pem=pub_pem, repo=repo)
    assert result2.ok is False
    assert result2.code == "STATEMENT_SHAPE"


def test_verify_bundle_subject_shape(tmp_path: Path, repo: Path) -> None:
    state_dir = tmp_path / "state"
    bundle, pub_pem, _, _ = setup_verified_bundle_fixture(state_dir, repo)
    res = verify_bundle(bundle, public_key_pem=pub_pem, repo=repo)
    assert res.statement is not None

    stmt = copy.deepcopy(res.statement)
    stmt["subject"] = []
    tampered_path = _resign_statement(state_dir, stmt, tmp_path / "b_subj.json")
    result = verify_bundle(tampered_path, public_key_pem=pub_pem, repo=repo)
    assert result.ok is False
    assert result.code == "SUBJECT_SHAPE"

    stmt2 = copy.deepcopy(res.statement)
    stmt2["subject"] = [{"digest": {"gitCommit": "not-a-40-hex-oid"}}]
    tampered_path2 = _resign_statement(state_dir, stmt2, tmp_path / "b_subj2.json")
    result2 = verify_bundle(tampered_path2, public_key_pem=pub_pem, repo=repo)
    assert result2.ok is False
    assert result2.code == "SUBJECT_SHAPE"


def test_verify_bundle_commit_not_found(tmp_path: Path, repo: Path) -> None:
    state_dir = tmp_path / "state"
    bundle, pub_pem, _, _ = setup_verified_bundle_fixture(state_dir, repo)
    res = verify_bundle(bundle, public_key_pem=pub_pem, repo=repo)
    assert res.statement is not None

    stmt = copy.deepcopy(res.statement)
    ghost_commit = "9" * 40
    stmt["subject"][0]["digest"]["gitCommit"] = ghost_commit
    stmt["subject"][0]["name"] = f"git-commit:{ghost_commit}"
    tampered_path = _resign_statement(state_dir, stmt, tmp_path / "b_ghost.json")
    result = verify_bundle(tampered_path, public_key_pem=pub_pem, repo=repo)
    assert result.ok is False
    assert result.code == "COMMIT_NOT_FOUND"


def test_verify_bundle_request_hash_mismatch(tmp_path: Path, repo: Path) -> None:
    state_dir = tmp_path / "state"
    bundle, pub_pem, _, _ = setup_verified_bundle_fixture(state_dir, repo)
    res = verify_bundle(bundle, public_key_pem=pub_pem, repo=repo)
    assert res.statement is not None

    stmt = copy.deepcopy(res.statement)
    stmt["predicate"]["request_hash"] = "sha256:" + "0" * 64
    tampered_path = _resign_statement(state_dir, stmt, tmp_path / "b_req_hash.json")
    result = verify_bundle(tampered_path, public_key_pem=pub_pem, repo=repo)
    assert result.ok is False
    assert result.code == "REQUEST_HASH_MISMATCH"


def test_verify_bundle_commit_binding_mismatch(tmp_path: Path, repo: Path) -> None:
    state_dir = tmp_path / "state"
    bundle, pub_pem, _, _ = setup_verified_bundle_fixture(state_dir, repo)
    res = verify_bundle(bundle, public_key_pem=pub_pem, repo=repo)
    assert res.statement is not None

    stmt = copy.deepcopy(res.statement)
    stmt["predicate"]["request"]["change_set"]["target_revision"] = "git-commit:" + "1" * 40
    stmt["predicate"]["request_hash"] = _digest(stmt["predicate"]["request"])
    tampered_path = _resign_statement(state_dir, stmt, tmp_path / "b_bind.json")
    result = verify_bundle(tampered_path, public_key_pem=pub_pem, repo=repo)
    assert result.ok is False
    assert result.code == "COMMIT_BINDING_MISMATCH"


def test_verify_bundle_manifest_mismatch(tmp_path: Path, repo: Path) -> None:
    state_dir = tmp_path / "state"
    bundle, pub_pem, _, _ = setup_verified_bundle_fixture(state_dir, repo)
    res = verify_bundle(bundle, public_key_pem=pub_pem, repo=repo)
    assert res.statement is not None

    stmt = copy.deepcopy(res.statement)
    stmt["predicate"]["request"]["change_set"]["diff_hash"] = "sha256:" + "0" * 64
    stmt["predicate"]["request_hash"] = _digest(stmt["predicate"]["request"])
    tampered_path = _resign_statement(state_dir, stmt, tmp_path / "b_diff.json")
    result = verify_bundle(tampered_path, public_key_pem=pub_pem, repo=repo)
    assert result.ok is False
    assert result.code == "MANIFEST_MISMATCH"


def test_verify_bundle_response_not_verified(tmp_path: Path, repo: Path) -> None:
    state_dir = tmp_path / "state"
    bundle, pub_pem, _, _ = setup_verified_bundle_fixture(state_dir, repo)
    res = verify_bundle(bundle, public_key_pem=pub_pem, repo=repo)
    assert res.statement is not None

    stmt = copy.deepcopy(res.statement)
    stmt["predicate"]["status"] = "FAILED_VERIFICATION"
    stmt["predicate"]["response"]["verification"]["status"] = "FAILED_VERIFICATION"
    tampered_path = _resign_statement(state_dir, stmt, tmp_path / "b_failed.json")
    result = verify_bundle(tampered_path, public_key_pem=pub_pem, repo=repo)
    assert result.ok is False
    assert result.code == "RESPONSE_NOT_VERIFIED"
