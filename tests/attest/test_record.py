"""Tests for cairnwork_attest.record."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from conftest import commit_change, git

from cairnwork.core_request import AcceptanceSpec, VerifierObservation
from cairnwork.notary import Notary, default_core_identity
from cairnwork_attest.record import (
    AttestError,
    RecordMalformed,
    RecordNotFound,
    RecordNotVerified,
    VerdictRecord,
    VerifiedRecord,
    read_record,
    verify_record,
)
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


def _tamper_db(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TRIGGER IF EXISTS core_verifications_no_update")
    conn.execute(sql, params)
    conn.commit()
    conn.close()


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


@pytest.mark.parametrize(
    ("task_id", "attempt_id"),
    [("task-nonexistent", "a1"), ("task-1", "attempt-nonexistent")],
)
def test_read_record_missing_task_or_attempt_raises_record_not_found(
    tmp_path: Path, repo: Path, task_id: str, attempt_id: str
) -> None:
    db_path = tmp_path / "notary.sqlite"
    setup_sample_notary(db_path, repo, "task-1", "a1")
    with pytest.raises(RecordNotFound) as exc_info:
        read_record(db_path, task_id, attempt_id)
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


@pytest.mark.parametrize(
    ("sql", "expected_code"),
    [
        ("UPDATE core_verifications SET request_json = 'not-valid-json{'", "INVALID_REQUEST_JSON"),
        ("UPDATE core_verifications SET response_json = 'invalid-json{'", "INVALID_RESPONSE_JSON"),
        ("UPDATE core_verifications SET request_json = '[1, 2, 3]'", "INVALID_REQUEST_STRUCTURE"),
    ],
)
def test_read_record_malformed_json_raises_record_malformed(
    tmp_path: Path, repo: Path, sql: str, expected_code: str
) -> None:
    db_path = tmp_path / "notary.sqlite"
    setup_sample_notary(db_path, repo, "task-1", "a1")
    _tamper_db(db_path, sql)

    with pytest.raises(RecordMalformed) as exc_info:
        read_record(db_path, "task-1", "a1")
    assert exc_info.value.code == expected_code


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


@pytest.mark.parametrize(
    ("req_json", "resp_json", "expected_code"),
    [
        ("invalid-json{", "{}", "INVALID_REQUEST_JSON"),
        ("{}", "[1, 2, 3]", "INVALID_RESPONSE_STRUCTURE"),
    ],
)
def test_verdict_record_post_init_malformed_json_raises(
    req_json: str, resp_json: str, expected_code: str
) -> None:
    with pytest.raises(RecordMalformed) as exc_info:
        VerdictRecord(
            task_id="t1",
            attempt_id="a1",
            base_commit="0" * 40,
            candidate_commit="1" * 40,
            request_hash="sha256:" + "a" * 64,
            request_json=req_json,
            http_status=200,
            status="VERIFIED",
            integrity="VALID",
            response_json=resp_json,
            recorded_at="2026-10-22T09:00:00Z",
        )
    assert exc_info.value.code == expected_code


def test_json_strictness_duplicate_key_raises_record_malformed() -> None:
    with pytest.raises(RecordMalformed) as exc_info:
        VerdictRecord(
            task_id="t1",
            attempt_id="a1",
            base_commit="0" * 40,
            candidate_commit="1" * 40,
            request_hash="sha256:" + "a" * 64,
            request_json='{"key": 1, "key": 2}',
            http_status=200,
            status="VERIFIED",
            integrity="VALID",
            response_json="{}",
            recorded_at="2026-10-22T09:00:00Z",
        )
    assert exc_info.value.code == "DUPLICATE_KEY"

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
            response_json='{"nested": {"foo": "bar", "foo": "baz"}}',
            recorded_at="2026-10-22T09:00:00Z",
        )
    assert exc_info.value.code == "DUPLICATE_KEY"


@pytest.mark.parametrize("constant_str", ["NaN", "Infinity", "-Infinity"])
def test_json_strictness_nan_infinity_raises_record_malformed(constant_str: str) -> None:
    with pytest.raises(RecordMalformed) as exc_info:
        VerdictRecord(
            task_id="t1",
            attempt_id="a1",
            base_commit="0" * 40,
            candidate_commit="1" * 40,
            request_hash="sha256:" + "a" * 64,
            request_json=f'{{"val": {constant_str}}}',
            http_status=200,
            status="VERIFIED",
            integrity="VALID",
            response_json="{}",
            recorded_at="2026-10-22T09:00:00Z",
        )
    assert exc_info.value.code == "RECORD_MALFORMED"


def test_verify_record_success(tmp_path: Path, repo: Path) -> None:
    db_path = tmp_path / "notary.sqlite"
    _, verdict = setup_sample_notary(db_path, repo, "task-1", "a1")

    verified = verify_record(db_path, "task-1", "a1", expected_candidate=verdict.candidate_commit)
    assert isinstance(verified, VerifiedRecord)
    assert verified.verdict.verified is True
    assert verified.record.candidate_commit == verdict.candidate_commit
    assert verified.record.task_id == "task-1"
    assert verified.record.attempt_id == "a1"


def test_verify_record_wrong_expected_candidate_raises_candidate_mismatch(
    tmp_path: Path, repo: Path
) -> None:
    db_path = tmp_path / "notary.sqlite"
    _, verdict = setup_sample_notary(db_path, repo, "task-1", "a1")
    wrong_candidate = "0" * 40
    assert wrong_candidate != verdict.candidate_commit

    with pytest.raises(RecordNotVerified) as exc_info:
        verify_record(db_path, "task-1", "a1", expected_candidate=wrong_candidate)
    assert exc_info.value.code == "CANDIDATE_MISMATCH"


@pytest.mark.parametrize("bad_cand", ["not-hex", "0" * 39, "0" * 41, "g" * 40, ""])
def test_verify_record_invalid_expected_candidate_raises_record_malformed(
    tmp_path: Path, repo: Path, bad_cand: str
) -> None:
    db_path = tmp_path / "notary.sqlite"
    setup_sample_notary(db_path, repo, "task-1", "a1")
    with pytest.raises(RecordMalformed):
        verify_record(db_path, "task-1", "a1", expected_candidate=bad_cand)


def test_verified_record_forged_construction_raises_attest_error(
    tmp_path: Path, repo: Path
) -> None:
    db_path = tmp_path / "notary.sqlite"
    _, verdict = setup_sample_notary(db_path, repo, "task-1", "a1")
    record = read_record(db_path, "task-1", "a1")

    ctor: Any = VerifiedRecord
    for args, kwargs in [
        ((record, verdict), {}),
        ((), {"record": record, "verdict": verdict}),
        ((record, verdict), {"_token": "forged"}),
        ((), {}),
        (("anything",), {}),
    ]:
        with pytest.raises(AttestError) as exc_info:
            ctor(*args, **kwargs)
        assert exc_info.value.code == "VERIFIED_RECORD_FORGED"


def test_verify_record_forged_request_hash_raises_record_malformed(
    tmp_path: Path, repo: Path
) -> None:
    db_path = tmp_path / "notary.sqlite"
    _, verdict = setup_sample_notary(db_path, repo, "task-1", "a1")
    _tamper_db(
        db_path,
        "UPDATE core_verifications SET request_hash = 'sha256:' || hex(zeroblob(32))",
    )

    with pytest.raises(RecordMalformed) as exc_info:
        verify_record(db_path, "task-1", "a1", expected_candidate=verdict.candidate_commit)
    assert exc_info.value.code == "RECORD_MALFORMED"


def test_verify_record_tampered_candidate_commit_raises_candidate_mismatch(
    tmp_path: Path, repo: Path
) -> None:
    db_path = tmp_path / "notary.sqlite"
    _, verdict = setup_sample_notary(db_path, repo, "task-1", "a1")
    sql = "UPDATE core_verifications SET candidate_commit = '" + "e" * 39 + "1'"
    _tamper_db(db_path, sql)

    with pytest.raises(RecordNotVerified) as exc_info:
        verify_record(db_path, "task-1", "a1", expected_candidate=verdict.candidate_commit)
    assert exc_info.value.code == "CANDIDATE_MISMATCH"


def test_verify_record_base_mismatch_raises_record_malformed(tmp_path: Path, repo: Path) -> None:
    db_path = tmp_path / "notary.sqlite"
    _, verdict = setup_sample_notary(db_path, repo, "task-1", "a1")
    _tamper_db(
        db_path,
        "UPDATE core_verifications SET base_commit = '" + "f" * 39 + "0'",
    )

    with pytest.raises(RecordMalformed) as exc_info:
        verify_record(db_path, "task-1", "a1", expected_candidate=verdict.candidate_commit)
    assert exc_info.value.code == "RECORD_MALFORMED"


def test_verify_record_failed_verification_raises_record_not_verified(
    tmp_path: Path, repo: Path
) -> None:
    db_path = tmp_path / "notary.sqlite"
    _, verdict = setup_sample_notary(db_path, repo, "task-1", "a1")
    _tamper_db(db_path, "UPDATE core_verifications SET status = 'FAILED_VERIFICATION'")

    with pytest.raises(RecordNotVerified) as exc_info:
        verify_record(db_path, "task-1", "a1", expected_candidate=verdict.candidate_commit)
    assert exc_info.value.code == "RECORD_NOT_VERIFIED"


def test_verify_record_non_200_raises_record_not_verified(tmp_path: Path, repo: Path) -> None:
    db_path = tmp_path / "notary.sqlite"
    _, verdict = setup_sample_notary(db_path, repo, "task-1", "a1")
    _tamper_db(db_path, "UPDATE core_verifications SET http_status = 500")

    with pytest.raises(RecordNotVerified) as exc_info:
        verify_record(db_path, "task-1", "a1", expected_candidate=verdict.candidate_commit)
    assert exc_info.value.code == "RECORD_NOT_VERIFIED"


def test_verify_record_invalid_integrity_raises_record_not_verified(
    tmp_path: Path, repo: Path
) -> None:
    db_path = tmp_path / "notary.sqlite"
    _, verdict = setup_sample_notary(db_path, repo, "task-1", "a1")
    _tamper_db(db_path, "UPDATE core_verifications SET integrity = 'INVALID'")

    with pytest.raises(RecordNotVerified) as exc_info:
        verify_record(db_path, "task-1", "a1", expected_candidate=verdict.candidate_commit)
    assert exc_info.value.code == "RECORD_NOT_VERIFIED"


def test_verify_record_unbound_response_hashes_raises_record_not_verified(
    tmp_path: Path, repo: Path
) -> None:
    db_path = tmp_path / "notary.sqlite"
    _, verdict = setup_sample_notary(db_path, repo, "task-1", "a1")

    conn = sqlite3.connect(db_path)
    raw_resp = conn.execute("SELECT response_json FROM core_verifications").fetchone()[0]
    conn.close()

    resp_dict = json.loads(raw_resp)
    resp_dict["hashes"]["change_manifest_hash"] = "sha256:" + "0" * 64
    _tamper_db(db_path, "UPDATE core_verifications SET response_json = ?", (json.dumps(resp_dict),))

    with pytest.raises(RecordNotVerified) as exc_info:
        verify_record(db_path, "task-1", "a1", expected_candidate=verdict.candidate_commit)
    assert exc_info.value.code == "RECORD_NOT_VERIFIED"
