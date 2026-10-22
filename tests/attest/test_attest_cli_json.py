"""Tests for cairnwork_attest.cli verify --json."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from conftest import git

from cairnwork_attest.cli import main
from tests.attest.test_attest_cli import setup_cli_fixture


def _setup_signed_bundle(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[str, str, Path, Path]:
    state = tmp_path / "state"
    base_commit = git(repo, "rev-parse", "HEAD")
    candidate = setup_cli_fixture(state, repo)
    main(["keygen", "--state", str(state)])
    out_keygen = capsys.readouterr().out
    split_pem = out_keygen.split("-----BEGIN PUBLIC KEY-----")[1]
    pub_pem_str = f"-----BEGIN PUBLIC KEY-----{split_pem}"
    pub_pem_file = tmp_path / "pub.pem"
    pub_pem_file.write_text(pub_pem_str, encoding="ascii")

    bundle_file = tmp_path / "bundle.json"
    rc_stmt = main(
        [
            "statement",
            "--state",
            str(state),
            "--task-id",
            "task-1",
            "--attempt-id",
            "a1",
            "--candidate",
            candidate,
            "--out",
            str(bundle_file),
            "--sign",
        ]
    )
    assert rc_stmt == 0
    capsys.readouterr()
    return candidate, base_commit, pub_pem_file, bundle_file


def test_verify_json_ok(tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    candidate, base_commit, pub_pem_file, bundle_file = _setup_signed_bundle(tmp_path, repo, capsys)
    rc = main(
        [
            "verify",
            str(bundle_file),
            "--public-key",
            str(pub_pem_file),
            "--repo",
            str(repo),
            "--candidate",
            candidate,
            "--json",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    lines = captured.out.splitlines(keepends=True)
    assert len(lines) == 1
    assert captured.out.endswith("\n")
    assert "Candidate:" not in captured.err

    data = json.loads(lines[0])
    assert data["ok"] is True
    assert data["code"] == "OK"
    assert data["candidate"] == candidate
    assert data["base"] == base_commit
    assert data["core_status"] == "VERIFIED"
    assert data["detail"] is None
    assert isinstance(data["keyid"], str) and len(data["keyid"]) > 0

    raw_bundle = json.loads(bundle_file.read_text(encoding="utf-8"))
    raw_payload = base64.b64decode(raw_bundle["payload"], validate=True)
    expected_sha = hashlib.sha256(raw_payload).hexdigest()
    assert len(data["statement_sha256"]) == 64
    assert data["statement_sha256"] == expected_sha


def test_verify_json_candidate_mismatch(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    candidate, base_commit, pub_pem_file, bundle_file = _setup_signed_bundle(tmp_path, repo, capsys)
    mismatched_candidate = "0" * 40
    rc = main(
        [
            "verify",
            str(bundle_file),
            "--public-key",
            str(pub_pem_file),
            "--repo",
            str(repo),
            "--candidate",
            mismatched_candidate,
            "--json",
        ]
    )
    assert rc == 1
    captured = capsys.readouterr()
    lines = captured.out.splitlines(keepends=True)
    assert len(lines) == 1
    assert captured.out.endswith("\n")
    assert "Candidate:" not in captured.err

    data = json.loads(lines[0])
    assert data["ok"] is False
    assert data["code"] == "CANDIDATE_MISMATCH"
    assert data["candidate"] == mismatched_candidate
    assert data["base"] == base_commit
    assert data["core_status"] == "VERIFIED"
    assert isinstance(data["keyid"], str) and len(data["keyid"]) > 0
    assert data["detail"] is not None

    raw_bundle = json.loads(bundle_file.read_text(encoding="utf-8"))
    raw_payload = base64.b64decode(raw_bundle["payload"], validate=True)
    assert data["statement_sha256"] == hashlib.sha256(raw_payload).hexdigest()


def test_verify_json_malformed_bundle(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    candidate, _, pub_pem_file, _ = _setup_signed_bundle(tmp_path, repo, capsys)
    bad_bundle = tmp_path / "bad_bundle.json"
    bad_bundle.write_text(
        '{"payload": "!!!", "payloadType": "x", "signatures": []}',
        encoding="utf-8",
    )
    rc = main(
        [
            "verify",
            str(bad_bundle),
            "--public-key",
            str(pub_pem_file),
            "--repo",
            str(repo),
            "--candidate",
            candidate,
            "--json",
        ]
    )
    assert rc == 1
    captured = capsys.readouterr()
    lines = captured.out.splitlines(keepends=True)
    assert len(lines) == 1
    assert captured.out.endswith("\n")
    assert "Candidate:" not in captured.err

    data = json.loads(lines[0])
    assert data["ok"] is False
    assert data["base"] is None
    assert data["statement_sha256"] is None
    assert data["core_status"] is None
    assert data["keyid"] is None
    assert data["candidate"] == candidate


def test_verify_json_malformed_candidate(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, _, pub_pem_file, bundle_file = _setup_signed_bundle(tmp_path, repo, capsys)
    rc = main(
        [
            "verify",
            str(bundle_file),
            "--public-key",
            str(pub_pem_file),
            "--repo",
            str(repo),
            "--candidate",
            "not-a-valid-sha",
            "--json",
        ]
    )
    assert rc == 2
    captured = capsys.readouterr()
    lines = captured.out.splitlines(keepends=True)
    assert len(lines) == 1
    assert captured.out.endswith("\n")
    assert "ERROR: malformed --candidate" in captured.err

    data = json.loads(lines[0])
    assert data["ok"] is False
    assert data["code"] == "USAGE"
    assert data["candidate"] == "not-a-valid-sha"
    assert data["base"] is None
    assert data["statement_sha256"] is None
    assert data["core_status"] is None
    assert data["keyid"] is None
    assert data["detail"] is not None


def test_verify_non_json_regression(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    candidate, _, pub_pem_file, bundle_file = _setup_signed_bundle(tmp_path, repo, capsys)
    rc = main(
        [
            "verify",
            str(bundle_file),
            "--public-key",
            str(pub_pem_file),
            "--repo",
            str(repo),
            "--candidate",
            candidate,
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == f"OK {candidate}\n"
    assert f"Candidate: {candidate}\n" in captured.err


def test_verify_json_unreadable_core_identity(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    candidate, _, pub_pem_file, bundle_file = _setup_signed_bundle(tmp_path, repo, capsys)
    nonexistent = tmp_path / "missing_core_id.json"
    rc = main(
        [
            "verify",
            str(bundle_file),
            "--public-key",
            str(pub_pem_file),
            "--repo",
            str(repo),
            "--candidate",
            candidate,
            "--core-identity",
            str(nonexistent),
            "--json",
        ]
    )
    assert rc == 2
    captured = capsys.readouterr()
    lines = captured.out.splitlines(keepends=True)
    assert len(lines) == 1
    assert captured.out.endswith("\n")
    assert "ERROR: cannot read core-identity" in captured.err

    data = json.loads(lines[0])
    assert data["ok"] is False
    assert data["code"] == "USAGE"
    assert data["candidate"] == candidate
    assert data["base"] is None
    assert data["statement_sha256"] is None
    assert data["core_status"] is None
    assert data["keyid"] is None
    assert "cannot read core-identity" in (data["detail"] or "")
