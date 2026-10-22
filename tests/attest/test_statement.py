"""Tests for cairnwork_attest.statement."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from conftest import commit_change, git

from cairnwork.core_request import AcceptanceSpec, VerifierObservation
from cairnwork.notary import Notary
from cairnwork_attest.assurance import Assurance
from cairnwork_attest.record import VerdictRecord, read_record
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


def test_round_trip_read_to_statement(tmp_path: Path, repo: Path) -> None:
    db_path = tmp_path / "notary.sqlite"
    setup_sample_notary(db_path, repo, "task-1", "a1")
    record = read_record(db_path, "task-1", "a1")

    producer = Producer(name="cairnwork", version="0.0.1.dev0", git_commit="c" * 40)
    assurance = Assurance(sandbox_backend="seatbelt", verifier_account_isolated=True)
    timestamp = "2026-10-22T09:00:00Z"

    statement = build_statement(
        record,
        producer=producer,
        assurance=assurance,
        timestamp=timestamp,
    )

    assert statement["_type"] == "https://in-toto.io/Statement/v1"
    assert statement["predicateType"] == PREDICATE_TYPE

    subjects = statement["subject"]
    assert len(subjects) == 2
    assert subjects[0]["name"] == f"git-commit:{record.candidate_commit}"
    assert subjects[0]["digest"] == {"gitCommit": record.candidate_commit}
    assert subjects[1]["name"] == f"git-commit:{record.base_commit}"
    assert subjects[1]["digest"] == {"gitCommit": record.base_commit}

    pred = statement["predicate"]
    assert pred["request"] == record.request
    assert pred["response"] == record.response
    assert pred["request_hash"] == record.request_hash
    assert pred["http_status"] == 200
    assert pred["status"] == "VERIFIED"
    assert pred["integrity"] == "VALID"
    assert pred["core_identity"] == CORE_IDENTITY
    assert pred["verifier_observations"] == record.request["evidence_bundle"]["observations"]
    assert pred["hidden_test_commit"] is None
    assert pred["producer"] == {
        "name": "cairnwork",
        "version": "0.0.1.dev0",
        "git_commit": "c" * 40,
    }
    assert pred["recorded_at"] == record.recorded_at
    assert pred["timestamp"] == timestamp
    assert pred["assurance"] == assurance.to_dict()


def test_statement_hash_stable_across_two_builds(tmp_path: Path, repo: Path) -> None:
    db_path = tmp_path / "notary.sqlite"
    setup_sample_notary(db_path, repo, "task-1", "a1")
    record = read_record(db_path, "task-1", "a1")

    producer = Producer(name="cairnwork", version="0.0.1.dev0", git_commit="c" * 40)
    assurance = Assurance(sandbox_backend="seatbelt")
    ts = "2026-10-22T09:00:00Z"

    s1 = build_statement(record, producer=producer, assurance=assurance, timestamp=ts)
    s2 = build_statement(record, producer=producer, assurance=assurance, timestamp=ts)

    assert statement_hash(s1) == statement_hash(s2)
    assert statement_bytes(s1) == statement_bytes(s2)
    assert statement_bytes(s1) == canonical_json(s1).encode("utf-8")


@pytest.mark.parametrize(
    ("field_name", "modified_val"),
    [
        ("http_status", 500),
        ("status", "FAILED_VERIFICATION"),
        ("integrity", "INVALID"),
        ("request_hash", "sha256:" + "9" * 64),
        ("timestamp", "2026-10-22T09:00:01Z"),
    ],
)
def test_statement_hash_changes_when_predicate_field_changes(
    tmp_path: Path, repo: Path, field_name: str, modified_val: Any
) -> None:
    db_path = tmp_path / "notary.sqlite"
    setup_sample_notary(db_path, repo, "task-1", "a1")
    record = read_record(db_path, "task-1", "a1")

    producer = Producer(name="cairnwork", version="0.0.1.dev0", git_commit="c" * 40)
    assurance = Assurance(sandbox_backend="seatbelt")
    base_ts = "2026-10-22T09:00:00Z"

    base_statement = build_statement(
        record, producer=producer, assurance=assurance, timestamp=base_ts
    )
    base_hash = statement_hash(base_statement)

    if field_name == "timestamp":
        mod_statement = build_statement(
            record, producer=producer, assurance=assurance, timestamp=modified_val
        )
    else:
        mod_record = replace(record, **{field_name: modified_val})
        mod_statement = build_statement(
            mod_record, producer=producer, assurance=assurance, timestamp=base_ts
        )

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
    setup_sample_notary(db_path, repo, "task-1", "a1")
    record = read_record(db_path, "task-1", "a1")

    producer = Producer()
    assurance = Assurance()
    ts = "2026-10-22T09:00:00Z"

    bad_cand = replace(record, candidate_commit=bad_commit)
    with pytest.raises(StatementInputError):
        build_statement(bad_cand, producer=producer, assurance=assurance, timestamp=ts)

    bad_base = replace(record, base_commit=bad_commit)
    with pytest.raises(StatementInputError):
        build_statement(bad_base, producer=producer, assurance=assurance, timestamp=ts)


def test_build_statement_missing_core_identity_and_observations() -> None:
    record = VerdictRecord(
        task_id="task-1",
        attempt_id="a1",
        base_commit="0" * 40,
        candidate_commit="1" * 40,
        request_hash="sha256:" + "a" * 64,
        request_json="{}",
        http_status=200,
        status="VERIFIED",
        integrity="VALID",
        response_json="{}",
        recorded_at="2026-10-22T09:00:00Z",
        request={"evidence_bundle": {}},
        response={"verification": {"status": "VERIFIED"}},
    )
    statement = build_statement(
        record,
        producer=Producer(),
        assurance=Assurance(),
        timestamp="2026-10-22T09:00:00Z",
    )
    assert statement["predicate"]["core_identity"] is None
    assert statement["predicate"]["verifier_observations"] == []
