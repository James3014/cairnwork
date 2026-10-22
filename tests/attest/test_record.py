"""Tests for cairnwork_attest.record."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from conftest import commit_change, git

from cairnwork.core_request import AcceptanceSpec, VerifierObservation
from cairnwork.notary import Notary
from cairnwork_attest.record import (
    RecordMalformed,
    RecordNotFound,
    VerdictRecord,
    read_record,
)
from cairnwork_protocol import (
    acceptance_contract_hash,
    change_manifest_hash,
    change_set_hash,
    evidence_bundle_hash,
    verification_plan_hash,
)

CORE_IDENTITY = {
    "core_version": "0.1.0-test",
    "deployment_id": "test-deployment",
}


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


def test_read_record_success(tmp_path: Path, repo: Path) -> None:
    db_path = tmp_path / "notary.sqlite"
    _, verdict = setup_sample_notary(db_path, repo, "task-1", "a1")

    record = read_record(db_path, "task-1", "a1")
    assert record.task_id == "task-1"
    assert record.attempt_id == "a1"
    assert record.base_commit == verdict.base_commit
    assert record.candidate_commit == verdict.candidate_commit
    assert record.request_hash == verdict.request_hash
    assert record.http_status == 200
    assert record.status == "VERIFIED"
    assert record.integrity == "VALID"
    assert isinstance(record.request, dict)
    assert record.request["acceptance_contract"]["contract_id"] == "task-1"
    assert isinstance(record.response, dict)
    assert record.response["verification"]["status"] == "VERIFIED"
    assert record.recorded_at


def test_read_record_missing_task_raises_record_not_found(tmp_path: Path, repo: Path) -> None:
    db_path = tmp_path / "notary.sqlite"
    setup_sample_notary(db_path, repo, "task-1", "a1")

    with pytest.raises(RecordNotFound) as exc_info:
        read_record(db_path, "task-nonexistent", "a1")
    assert exc_info.value.code == "RECORD_NOT_FOUND"


def test_read_record_missing_attempt_raises_record_not_found(tmp_path: Path, repo: Path) -> None:
    db_path = tmp_path / "notary.sqlite"
    setup_sample_notary(db_path, repo, "task-1", "a1")

    with pytest.raises(RecordNotFound) as exc_info:
        read_record(db_path, "task-1", "attempt-nonexistent")
    assert exc_info.value.code == "RECORD_NOT_FOUND"


def test_read_record_missing_database_raises_record_not_found(tmp_path: Path) -> None:
    db_path = tmp_path / "does_not_exist.sqlite"
    with pytest.raises(RecordNotFound) as exc_info:
        read_record(db_path, "task-1", "a1")
    assert exc_info.value.code == "RECORD_NOT_FOUND"


def test_read_record_missing_table_raises_record_not_found(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE other_table (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    with pytest.raises(RecordNotFound) as exc_info:
        read_record(db_path, "task-1", "a1")
    assert exc_info.value.code == "RECORD_NOT_FOUND"


def test_read_record_malformed_request_json_raises_record_malformed(
    tmp_path: Path, repo: Path
) -> None:
    db_path = tmp_path / "notary.sqlite"
    setup_sample_notary(db_path, repo, "task-1", "a1")

    conn = sqlite3.connect(db_path)
    conn.execute("DROP TRIGGER core_verifications_no_update")
    conn.execute("UPDATE core_verifications SET request_json = 'not-valid-json{'")
    conn.commit()
    conn.close()

    with pytest.raises(RecordMalformed) as exc_info:
        read_record(db_path, "task-1", "a1")
    assert exc_info.value.code == "INVALID_REQUEST_JSON"


def test_read_record_malformed_response_json_raises_record_malformed(
    tmp_path: Path, repo: Path
) -> None:
    db_path = tmp_path / "notary.sqlite"
    setup_sample_notary(db_path, repo, "task-1", "a1")

    conn = sqlite3.connect(db_path)
    conn.execute("DROP TRIGGER core_verifications_no_update")
    conn.execute("UPDATE core_verifications SET response_json = 'invalid-json{'")
    conn.commit()
    conn.close()

    with pytest.raises(RecordMalformed) as exc_info:
        read_record(db_path, "task-1", "a1")
    assert exc_info.value.code == "INVALID_RESPONSE_JSON"


def test_read_record_non_object_json_raises_record_malformed(tmp_path: Path, repo: Path) -> None:
    db_path = tmp_path / "notary.sqlite"
    setup_sample_notary(db_path, repo, "task-1", "a1")

    conn = sqlite3.connect(db_path)
    conn.execute("DROP TRIGGER core_verifications_no_update")
    conn.execute("UPDATE core_verifications SET request_json = '[1, 2, 3]'")
    conn.commit()
    conn.close()

    with pytest.raises(RecordMalformed) as exc_info:
        read_record(db_path, "task-1", "a1")
    assert exc_info.value.code == "INVALID_REQUEST_STRUCTURE"


def test_verdict_record_post_init_parses_json() -> None:
    vr = VerdictRecord(
        task_id="t1",
        attempt_id="a1",
        base_commit="0" * 40,
        candidate_commit="1" * 40,
        request_hash="sha256:" + "a" * 64,
        request_json='{"req": 1}',
        http_status=200,
        status="VERIFIED",
        integrity="VALID",
        response_json='{"resp": 2}',
        recorded_at="2026-10-22T09:00:00Z",
    )
    assert vr.request == {"req": 1}
    assert vr.response == {"resp": 2}


def test_verdict_record_post_init_malformed_json_raises() -> None:
    with pytest.raises(RecordMalformed) as exc_info:
        VerdictRecord(
            task_id="t1",
            attempt_id="a1",
            base_commit="0" * 40,
            candidate_commit="1" * 40,
            request_hash="sha256:" + "a" * 64,
            request_json="invalid-json{",
            http_status=200,
            status="VERIFIED",
            integrity="VALID",
            response_json="{}",
            recorded_at="2026-10-22T09:00:00Z",
        )
    assert exc_info.value.code == "INVALID_REQUEST_JSON"


def test_verdict_record_post_init_non_object_json_raises() -> None:
    with pytest.raises(RecordMalformed) as exc_info:
        VerdictRecord(
            task_id="t1",
            attempt_id="a1",
            base_commit="0" * 40,
            candidate_commit="1" * 40,
            request_hash="sha256:" + "a" * 64,
            request_json="{}",
            http_status=200,
            status="VERIFIED",
            integrity="VALID",
            response_json="[1, 2, 3]",
            recorded_at="2026-10-22T09:00:00Z",
        )
    assert exc_info.value.code == "INVALID_RESPONSE_STRUCTURE"
