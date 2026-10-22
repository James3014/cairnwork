"""Tests for cairnwork_attest.statement."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from conftest import commit_change, git

from cairnwork.core_request import AcceptanceSpec, VerifierObservation
from cairnwork.notary import Notary, default_core_identity
from cairnwork_attest.record import AttestError, RecordNotVerified, _digest, verify_record
from cairnwork_attest.statement import (
    PREDICATE_TYPE,
    Producer,
    StatementInputError,
    build_statement,
    statement_bytes,
    statement_hash,
)
from cairnwork_protocol import (
    acceptance_contract_hash,
    canonical_json,
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


def test_round_trip_read_to_statement(tmp_path: Path, repo: Path) -> None:
    db_path = tmp_path / "notary.sqlite"
    _, verdict = setup_sample_notary(db_path, repo, "task-1", "a1")
    verified = verify_record(db_path, "task-1", "a1", expected_candidate=verdict.candidate_commit)

    producer = Producer(name="cairnwork", version="0.0.1.dev0", git_commit="c" * 40)
    timestamp = "2026-10-22T09:00:00Z"

    statement = build_statement(
        verified,
        producer=producer,
        timestamp=timestamp,
    )

    assert statement["_type"] == "https://in-toto.io/Statement/v1"
    assert statement["predicateType"] == PREDICATE_TYPE

    subjects = statement["subject"]
    assert len(subjects) == 1
    assert subjects[0]["name"] == f"git-commit:{verified.record.candidate_commit}"
    assert subjects[0]["digest"] == {"gitCommit": verified.record.candidate_commit}

    pred = statement["predicate"]
    assert pred["base_commit"] == verified.record.base_commit
    assert pred["request"] == verified.record.request
    assert pred["response"] == verified.record.response
    assert pred["request_hash"] == verified.record.request_hash
    assert pred["http_status"] == 200
    assert pred["status"] == "VERIFIED"
    assert pred["integrity"] == "VALID"
    assert pred["core_identity"] == CORE_IDENTITY
    assert (
        pred["verifier_observations"] == verified.record.request["evidence_bundle"]["observations"]
    )
    assert pred["hidden_test_commit"] is None
    assert pred["producer"] == {
        "name": "cairnwork",
        "version": "0.0.1.dev0",
        "git_commit": "c" * 40,
    }
    assert pred["recorded_at"] == verified.record.recorded_at
    assert pred["timestamp"] == timestamp
    assert pred["assurance"] == {
        "sandbox_backend": "unknown",
        "verifier_account_isolated": "unknown",
        "worker_isolated": "unknown",
        "hidden_tests": "unknown",
        "network": "unknown",
    }


def test_statement_hash_stable_across_two_builds(tmp_path: Path, repo: Path) -> None:
    db_path = tmp_path / "notary.sqlite"
    _, verdict = setup_sample_notary(db_path, repo, "task-1", "a1")
    verified = verify_record(db_path, "task-1", "a1", expected_candidate=verdict.candidate_commit)

    producer = Producer(name="cairnwork", version="0.0.1.dev0", git_commit="c" * 40)
    ts = "2026-10-22T09:00:00Z"

    s1 = build_statement(verified, producer=producer, timestamp=ts)
    s2 = build_statement(verified, producer=producer, timestamp=ts)

    assert statement_hash(s1) == statement_hash(s2)
    assert statement_bytes(s1) == statement_bytes(s2)
    assert statement_bytes(s1) == canonical_json(s1).encode("utf-8")


@pytest.mark.parametrize(
    ("field_name", "modified_val"),
    [
        ("recorded_at", "2026-10-22T10:00:00Z"),
        ("timestamp", "2026-10-22T09:00:01Z"),
        ("http_status", 201),
    ],
)
def test_statement_hash_changes_when_predicate_field_changes(
    tmp_path: Path, repo: Path, field_name: str, modified_val: Any
) -> None:
    db_path = tmp_path / "notary.sqlite"
    _, verdict = setup_sample_notary(db_path, repo, "task-1", "a1")
    verified = verify_record(db_path, "task-1", "a1", expected_candidate=verdict.candidate_commit)

    producer = Producer(name="cairnwork", version="0.0.1.dev0", git_commit="c" * 40)
    base_ts = "2026-10-22T09:00:00Z"

    base_statement = build_statement(verified, producer=producer, timestamp=base_ts)
    base_hash = statement_hash(base_statement)

    if field_name == "timestamp":
        mod_statement = build_statement(verified, producer=producer, timestamp=modified_val)
    else:
        mod_record = replace(verified.record, **{field_name: modified_val})
        object.__setattr__(verified, "record", mod_record)
        mod_statement = build_statement(verified, producer=producer, timestamp=base_ts)

    assert statement_hash(mod_statement) != base_hash


@pytest.mark.parametrize(
    "bad_commit",
    [
        "not-a-hex",
        "0" * 39,
        "0" * 41,
        "g" * 40,
        "A" * 40,
        "",
    ],
)
def test_invalid_commit_raises_statement_input_error(
    tmp_path: Path, repo: Path, bad_commit: str
) -> None:
    db_path = tmp_path / "notary.sqlite"
    _, verdict = setup_sample_notary(db_path, repo, "task-1", "a1")
    verified = verify_record(db_path, "task-1", "a1", expected_candidate=verdict.candidate_commit)

    producer = Producer()
    ts = "2026-10-22T09:00:00Z"

    bad_cand = replace(verified.record, candidate_commit=bad_commit)
    object.__setattr__(verified, "record", bad_cand)
    with pytest.raises(StatementInputError):
        build_statement(verified, producer=producer, timestamp=ts)

    bad_base = replace(verified.record, base_commit=bad_commit)
    object.__setattr__(verified, "record", bad_base)
    with pytest.raises(StatementInputError):
        build_statement(verified, producer=producer, timestamp=ts)


def test_build_statement_unverified_verdict_raises_tampered(tmp_path: Path, repo: Path) -> None:
    db_path = tmp_path / "notary.sqlite"
    _, verdict = setup_sample_notary(db_path, repo, "task-1", "a1")
    verified = verify_record(db_path, "task-1", "a1", expected_candidate=verdict.candidate_commit)

    unverified = replace(verified.verdict, verified=False, status="FAILED_VERIFICATION")
    object.__setattr__(verified, "verdict", unverified)
    with pytest.raises(StatementInputError) as exc_info:
        build_statement(
            verified,
            producer=Producer(),
            timestamp="2026-10-22T09:00:00Z",
        )
    assert exc_info.value.code == "VERIFIED_RECORD_TAMPERED"


def test_build_statement_mismatched_verdict_raises_tampered(tmp_path: Path, repo: Path) -> None:
    db_path = tmp_path / "notary.sqlite"
    _, verdict = setup_sample_notary(db_path, repo, "task-1", "a1")
    verified = verify_record(db_path, "task-1", "a1", expected_candidate=verdict.candidate_commit)

    mismatched_task = replace(verified.verdict, task_id="other-task")
    object.__setattr__(verified, "verdict", mismatched_task)
    with pytest.raises(StatementInputError) as exc_info:
        build_statement(
            verified,
            producer=Producer(),
            timestamp="2026-10-22T09:00:00Z",
        )
    assert exc_info.value.code == "VERIFIED_RECORD_TAMPERED"

    mismatched_cand = replace(verified.verdict, candidate_commit="0" * 40)
    object.__setattr__(verified, "verdict", mismatched_cand)
    with pytest.raises(StatementInputError) as exc_info:
        build_statement(
            verified,
            producer=Producer(),
            timestamp="2026-10-22T09:00:00Z",
        )
    assert exc_info.value.code == "VERIFIED_RECORD_TAMPERED"


def test_build_statement_subject_has_exactly_one_entry(tmp_path: Path, repo: Path) -> None:
    db_path = tmp_path / "notary.sqlite"
    _, verdict = setup_sample_notary(db_path, repo, "task-1", "a1")
    verified = verify_record(db_path, "task-1", "a1", expected_candidate=verdict.candidate_commit)

    statement = build_statement(
        verified,
        producer=Producer(),
        timestamp="2026-10-22T09:00:00Z",
    )
    assert len(statement["subject"]) == 1
    assert statement["subject"][0]["name"] == f"git-commit:{verified.record.candidate_commit}"
    assert statement["subject"][0]["digest"] == {"gitCommit": verified.record.candidate_commit}
    assert statement["predicate"]["base_commit"] == verified.record.base_commit


def test_build_statement_bare_record_or_pair_raises_statement_input_error(
    tmp_path: Path, repo: Path
) -> None:
    db_path = tmp_path / "notary.sqlite"
    _, verdict = setup_sample_notary(db_path, repo, "task-1", "a1")
    verified = verify_record(db_path, "task-1", "a1", expected_candidate=verdict.candidate_commit)
    producer = Producer()
    ts = "2026-10-22T09:00:00Z"

    bad_inputs: list[Any] = [
        verified.record,
        (verified.record, verified.verdict),
        verified.verdict,
        "bare string",
        None,
    ]
    for bad in bad_inputs:
        with pytest.raises(StatementInputError) as exc_info:
            build_statement(bad, producer=producer, timestamp=ts)
        assert exc_info.value.code == "STATEMENT_INPUT_ERROR"


def test_verified_record_is_snapshot_after_db_tamper(tmp_path: Path, repo: Path) -> None:
    db_path = tmp_path / "notary.sqlite"
    _, verdict = setup_sample_notary(db_path, repo, "task-1", "a1")
    verified = verify_record(db_path, "task-1", "a1", expected_candidate=verdict.candidate_commit)

    conn = sqlite3.connect(db_path)
    conn.execute("DROP TRIGGER IF EXISTS core_verifications_no_update")
    conn.execute("UPDATE core_verifications SET status = 'FAILED_VERIFICATION'")
    conn.commit()
    conn.close()

    statement = build_statement(
        verified,
        producer=Producer(),
        timestamp="2026-10-22T09:00:00Z",
    )
    assert statement["predicate"]["status"] == "VERIFIED"

    with pytest.raises(RecordNotVerified) as exc_info:
        verify_record(db_path, "task-1", "a1", expected_candidate=verdict.candidate_commit)
    assert exc_info.value.code == "RECORD_NOT_VERIFIED"


def test_build_statement_missing_core_identity_and_observations(tmp_path: Path, repo: Path) -> None:
    db_path = tmp_path / "notary.sqlite"
    _, verdict = setup_sample_notary(db_path, repo, "task-1", "a1")
    verified = verify_record(db_path, "task-1", "a1", expected_candidate=verdict.candidate_commit)

    stripped_req = dict(verified.record.request)
    stripped_req["evidence_bundle"] = {}
    stripped_resp = dict(verified.record.response)
    stripped_resp.pop("core_identity", None)
    mod_record = replace(
        verified.record,
        request=stripped_req,
        response=stripped_resp,
        request_hash=_digest(stripped_req),
    )
    object.__setattr__(verified, "record", mod_record)

    statement = build_statement(
        verified,
        producer=Producer(),
        timestamp="2026-10-22T09:00:00Z",
    )
    assert statement["predicate"]["core_identity"] is None
    assert statement["predicate"]["verifier_observations"] == []


def test_verified_record_dataclass_replace_raises_forged(tmp_path: Path, repo: Path) -> None:
    db_path = tmp_path / "notary.sqlite"
    _, verdict = setup_sample_notary(db_path, repo, "task-1", "a1")
    verified = verify_record(db_path, "task-1", "a1", expected_candidate=verdict.candidate_commit)
    other_record = replace(verified.record, task_id="other-task")

    with pytest.raises(AttestError) as exc_info:
        replace(verified, record=other_record)
    assert exc_info.value.code == "VERIFIED_RECORD_FORGED"


def test_build_statement_swapped_inner_record_via_setattr_refused(
    tmp_path: Path, repo: Path
) -> None:
    db_path = tmp_path / "notary.sqlite"
    _, verdict = setup_sample_notary(db_path, repo, "task-1", "a1")
    verified = verify_record(db_path, "task-1", "a1", expected_candidate=verdict.candidate_commit)
    other_record = replace(verified.record, task_id="swapped-task")

    object.__setattr__(verified, "record", other_record)
    with pytest.raises(StatementInputError) as exc_info:
        build_statement(
            verified,
            producer=Producer(),
            timestamp="2026-10-22T09:00:00Z",
        )
    assert exc_info.value.code == "VERIFIED_RECORD_TAMPERED"


def test_build_statement_tampered_request_hash_via_setattr_refused(
    tmp_path: Path, repo: Path
) -> None:
    db_path = tmp_path / "notary.sqlite"
    _, verdict = setup_sample_notary(db_path, repo, "task-1", "a1")
    verified = verify_record(db_path, "task-1", "a1", expected_candidate=verdict.candidate_commit)
    tampered_record = replace(verified.record, request_hash="sha256:" + "0" * 64)

    object.__setattr__(verified, "record", tampered_record)
    with pytest.raises(StatementInputError) as exc_info:
        build_statement(
            verified,
            producer=Producer(),
            timestamp="2026-10-22T09:00:00Z",
        )
    assert exc_info.value.code == "VERIFIED_RECORD_TAMPERED"
