"""Tests for cairnwork_attest.cli."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest
from conftest import commit_change, git

from cairnwork.core_request import AcceptanceSpec, VerifierObservation
from cairnwork.notary import Notary, default_core_identity
from cairnwork_attest.cli import main
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


def setup_cli_fixture(
    state_dir: Path, repo: Path, task_id: str = "task-1", attempt_id: str = "a1"
) -> str:
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / "notary.sqlite"
    notary = Notary(db_path, core_verify=fake_core_verify, core_identity=CORE_IDENTITY)
    base_commit = git(repo, "rev-parse", "HEAD")
    candidate_commit = commit_change(repo, {"src/a.py": "x = 2\n"})
    spec = AcceptanceSpec(task_id, "sha256:" + "c" * 64, ("pytest",), ("src/a.py",), False)
    obs = (VerifierObservation("pytest", "artifact-1", "sha256:" + "e" * 64, True),)
    notary.notarize(task_id, attempt_id, spec, repo, base_commit, candidate_commit, obs)
    return candidate_commit


def test_cli_keygen_success_and_exists(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state = tmp_path / "state"
    state.mkdir()

    rc = main(["keygen", "--state", str(state)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "keyid: " in out
    assert "-----BEGIN PUBLIC KEY-----" in out
    assert (state / "attest_ed25519.pem").is_file()

    # Re-running keygen on same state dir fails (key exists)
    rc_dup = main(["keygen", "--state", str(state)])
    assert rc_dup == 1
    err = capsys.readouterr().err
    assert "key file already exists" in err


def test_cli_statement_unsigned(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    candidate = setup_cli_fixture(state, repo)
    out_file = tmp_path / "statement.json"

    rc = main(
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
            str(out_file),
        ]
    )
    assert rc == 0
    assert out_file.is_file()
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["_type"] == "https://in-toto.io/Statement/v1"
    assert data["predicate"]["status"] == "VERIFIED"


def test_cli_statement_signed_and_refuse_overwrite(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    candidate = setup_cli_fixture(state, repo)
    main(["keygen", "--state", str(state)])
    capsys.readouterr()

    out_file = tmp_path / "bundle.json"
    rc = main(
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
            str(out_file),
            "--sign",
        ]
    )
    assert rc == 0
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["payloadType"] == "application/vnd.in-toto+json"
    assert "signatures" in data

    # Refuse overwrite exits 2 with error printed to stderr
    rc_dup = main(
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
            str(out_file),
            "--sign",
        ]
    )
    assert rc_dup == 2
    err = capsys.readouterr().err
    assert "BUNDLE_EXISTS" in err


def test_cli_statement_candidate_mismatch(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    setup_cli_fixture(state, repo)
    out_file = tmp_path / "stmt.json"

    rc = main(
        [
            "statement",
            "--state",
            str(state),
            "--task-id",
            "task-1",
            "--attempt-id",
            "a1",
            "--candidate",
            "0" * 40,
            "--out",
            str(out_file),
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "CANDIDATE_MISMATCH" in err


def test_cli_verify_roundtrip_and_no_network(
    tmp_path: Path,
    repo: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_socket(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("network socket forbidden")

    monkeypatch.setattr(socket, "socket", fail_socket)

    state = tmp_path / "state"
    candidate = setup_cli_fixture(state, repo)

    main(["keygen", "--state", str(state)])
    out_keygen = capsys.readouterr().out
    pub_pem_str = out_keygen.split("-----BEGIN PUBLIC KEY-----")[1]
    pub_pem_str = "-----BEGIN PUBLIC KEY-----" + pub_pem_str
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

    # Verify succeeds
    rc_ver = main(
        [
            "verify",
            str(bundle_file),
            "--public-key",
            str(pub_pem_file),
            "--repo",
            str(repo),
        ]
    )
    assert rc_ver == 0
    out_ver = capsys.readouterr().out
    assert out_ver.strip() == f"OK {candidate}"

    # Tampered verification fails
    bundle_file.write_text('{"payloadType":"bad"}', encoding="utf-8")
    rc_bad = main(
        [
            "verify",
            str(bundle_file),
            "--public-key",
            str(pub_pem_file),
            "--repo",
            str(repo),
        ]
    )
    assert rc_bad == 1
    out_bad = capsys.readouterr().out
    assert "SIGNATURE_INVALID" in out_bad
