"""Negative tests for cairnwork_attest.verify, re-signed with the test key."""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path

import pytest

from cairnwork_attest.bundle import read_bundle, write_bundle
from cairnwork_attest.cli import main
from cairnwork_attest.record import _digest, parse_strict_json
from cairnwork_attest.verify import verify_bundle
from tests.attest.test_verify import (
    _resign_statement,
    setup_verified_bundle_fixture,
)


def test_negative_commit_binding_mismatch_source_revision(tmp_path: Path, repo: Path) -> None:
    state_dir = tmp_path / "state"
    bundle, pub_pem, _, candidate = setup_verified_bundle_fixture(state_dir, repo)
    res = verify_bundle(bundle, public_key_pem=pub_pem, repo=repo, expected_candidate=candidate)
    assert res.statement is not None

    stmt = copy.deepcopy(res.statement)
    stmt["predicate"]["request"]["change_set"]["source_revision"] = "git-commit:" + "1" * 40
    stmt["predicate"]["request_hash"] = _digest(stmt["predicate"]["request"])
    tampered_path = _resign_statement(state_dir, stmt, tmp_path / "b_src_rev.json")
    result = verify_bundle(
        tampered_path, public_key_pem=pub_pem, repo=repo, expected_candidate=candidate
    )
    assert result.ok is False
    assert result.code == "COMMIT_BINDING_MISMATCH"


def test_negative_http_status_not_200(tmp_path: Path, repo: Path) -> None:
    state_dir = tmp_path / "state"
    bundle, pub_pem, _, candidate = setup_verified_bundle_fixture(state_dir, repo)
    res = verify_bundle(bundle, public_key_pem=pub_pem, repo=repo, expected_candidate=candidate)
    assert res.statement is not None

    stmt = copy.deepcopy(res.statement)
    stmt["predicate"]["http_status"] = 500
    tampered_path = _resign_statement(state_dir, stmt, tmp_path / "b_http500.json")
    result = verify_bundle(
        tampered_path, public_key_pem=pub_pem, repo=repo, expected_candidate=candidate
    )
    assert result.ok is False
    assert result.code == "RESPONSE_NOT_VERIFIED"


@pytest.mark.parametrize(
    "hash_key",
    [
        "acceptance_contract_hash",
        "change_set_hash",
        "verification_plan_hash",
        "evidence_bundle_hash",
        "change_manifest_hash",
    ],
)
def test_negative_echoed_request_hashes_altered(tmp_path: Path, repo: Path, hash_key: str) -> None:
    state_dir = tmp_path / "state"
    bundle, pub_pem, _, candidate = setup_verified_bundle_fixture(state_dir, repo)
    res = verify_bundle(bundle, public_key_pem=pub_pem, repo=repo, expected_candidate=candidate)
    assert res.statement is not None

    stmt = copy.deepcopy(res.statement)
    stmt["predicate"]["response"]["hashes"][hash_key] = "sha256:" + "0" * 64
    tampered_path = _resign_statement(state_dir, stmt, tmp_path / f"b_{hash_key}.json")
    result = verify_bundle(
        tampered_path, public_key_pem=pub_pem, repo=repo, expected_candidate=candidate
    )
    assert result.ok is False
    assert result.code == "RESPONSE_NOT_VERIFIED"


def test_negative_manifest_entry_altered(tmp_path: Path, repo: Path) -> None:
    state_dir = tmp_path / "state"
    bundle, pub_pem, _, candidate = setup_verified_bundle_fixture(state_dir, repo)
    res = verify_bundle(bundle, public_key_pem=pub_pem, repo=repo, expected_candidate=candidate)
    assert res.statement is not None

    stmt = copy.deepcopy(res.statement)
    entries = stmt["predicate"]["request"]["change_manifest"]["entries"]
    assert len(entries) > 0
    entries[0]["after_oid"] = "0" * 40
    stmt["predicate"]["request_hash"] = _digest(stmt["predicate"]["request"])
    tampered_path = _resign_statement(state_dir, stmt, tmp_path / "b_manifest_entry.json")
    result = verify_bundle(
        tampered_path, public_key_pem=pub_pem, repo=repo, expected_candidate=candidate
    )
    assert result.ok is False
    assert result.code == "MANIFEST_MISMATCH"


def test_negative_core_identity_mismatch_in_predicate(tmp_path: Path, repo: Path) -> None:
    state_dir = tmp_path / "state"
    bundle, pub_pem, _, candidate = setup_verified_bundle_fixture(state_dir, repo)
    res = verify_bundle(bundle, public_key_pem=pub_pem, repo=repo, expected_candidate=candidate)
    assert res.statement is not None

    stmt = copy.deepcopy(res.statement)
    stmt["predicate"]["response"]["core_identity"] = {
        **stmt["predicate"]["core_identity"],
        "version": "9.9.9",
    }
    tampered_path = _resign_statement(state_dir, stmt, tmp_path / "b_core_id.json")
    result = verify_bundle(
        tampered_path, public_key_pem=pub_pem, repo=repo, expected_candidate=candidate
    )
    assert result.ok is False
    assert result.code == "RESPONSE_NOT_VERIFIED"


def test_negative_core_identity_pin_mismatch(tmp_path: Path, repo: Path) -> None:
    state_dir = tmp_path / "state"
    bundle, pub_pem, _, candidate = setup_verified_bundle_fixture(state_dir, repo)

    # 1. Matching pin passes
    envelope = read_bundle(bundle)
    raw_payload = base64.b64decode(envelope["payload"]).decode("utf-8")
    stmt = parse_strict_json(raw_payload)
    correct_pin = stmt["predicate"]["core_identity"]

    ok_result = verify_bundle(
        bundle,
        public_key_pem=pub_pem,
        repo=repo,
        expected_candidate=candidate,
        core_identity_pin=correct_pin,
    )
    assert ok_result.ok is True
    assert ok_result.code == "OK"

    # 2. Mismatched pin fails with CORE_IDENTITY_MISMATCH
    wrong_pin = {**correct_pin, "version": "0.0.0-untrusted"}
    fail_result = verify_bundle(
        bundle,
        public_key_pem=pub_pem,
        repo=repo,
        expected_candidate=candidate,
        core_identity_pin=wrong_pin,
    )
    assert fail_result.ok is False
    assert fail_result.code == "CORE_IDENTITY_MISMATCH"


def test_negative_extra_top_level_envelope_key(tmp_path: Path, repo: Path) -> None:
    state_dir = tmp_path / "state"
    bundle, pub_pem, _, candidate = setup_verified_bundle_fixture(state_dir, repo)
    envelope = read_bundle(bundle)
    envelope["extra_field"] = "not_in_whitelist"
    extra_path = tmp_path / "b_extra_key.json"
    write_bundle(extra_path, envelope)

    result = verify_bundle(
        extra_path, public_key_pem=pub_pem, repo=repo, expected_candidate=candidate
    )
    assert result.ok is False
    assert result.code == "ENVELOPE_SHAPE"


def test_negative_bundle_larger_than_max(tmp_path: Path, repo: Path) -> None:
    state_dir = tmp_path / "state"
    _, pub_pem, _, candidate = setup_verified_bundle_fixture(state_dir, repo)
    large_bundle = tmp_path / "too_large.json"
    large_bundle.write_bytes(b" " * (4 * 1024 * 1024 + 1))

    result = verify_bundle(
        large_bundle, public_key_pem=pub_pem, repo=repo, expected_candidate=candidate
    )
    assert result.ok is False
    assert result.code == "BUNDLE_UNREADABLE"


def test_cli_verify_with_core_identity_pin(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_dir = tmp_path / "state"
    bundle, pub_pem, _, candidate = setup_verified_bundle_fixture(state_dir, repo)

    envelope = read_bundle(bundle)
    raw_payload = base64.b64decode(envelope["payload"]).decode("utf-8")
    stmt = parse_strict_json(raw_payload)
    core_id = stmt["predicate"]["core_identity"]

    good_pin_path = tmp_path / "good_core_id.json"
    good_pin_path.write_text(json.dumps(core_id), encoding="utf-8")

    bad_pin_path = tmp_path / "bad_core_id.json"
    bad_pin_path.write_text(json.dumps({**core_id, "name": "rogue-core"}), encoding="utf-8")

    # Good pin succeeds
    assert (
        main(
            [
                "verify",
                str(bundle),
                "--public-key",
                str(pub_pem),
                "--repo",
                str(repo),
                "--candidate",
                candidate,
                "--core-identity",
                str(good_pin_path),
            ]
        )
        == 0
    )
    assert f"OK {candidate}" in capsys.readouterr().out

    # Bad pin fails with CORE_IDENTITY_MISMATCH
    assert (
        main(
            [
                "verify",
                str(bundle),
                "--public-key",
                str(pub_pem),
                "--repo",
                str(repo),
                "--candidate",
                candidate,
                "--core-identity",
                str(bad_pin_path),
            ]
        )
        == 1
    )
    out = capsys.readouterr().out
    assert "CORE_IDENTITY_MISMATCH" in out
