"""Tests for cairnwork_attest.assurance."""

from __future__ import annotations

from typing import Any

from cairnwork_attest.assurance import Assurance, assurance_from_record
from cairnwork_attest.record import VerdictRecord


def make_record(request: dict[str, Any] | None = None) -> VerdictRecord:
    return VerdictRecord(
        task_id="t1",
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
        request=request or {},
        response={},
    )


def test_assurance_never_returns_true_without_evidence() -> None:
    record = make_record()
    assurance = assurance_from_record(record)
    assert assurance.verifier_account_isolated is not True
    assert assurance.worker_isolated is not True
    assert assurance.hidden_tests is not True
    assert assurance.sandbox_backend == "unknown"
    assert assurance.network == "unknown"
    assert assurance.asserted_by_caller is False

    rendered = assurance.to_dict()
    assert rendered["verifier_account_isolated"] == "unknown"
    assert rendered["worker_isolated"] == "unknown"
    assert rendered["hidden_tests"] == "unknown"
    assert rendered["sandbox_backend"] == "unknown"
    assert rendered["network"] == "unknown"
    assert "asserted_by_caller" not in rendered


def test_assurance_from_record_reads_verifier_account_from_request() -> None:
    rec_true = make_record({"verifier_account": True})
    assert assurance_from_record(rec_true).verifier_account_isolated is True

    rec_false = make_record({"verifier_account": False})
    assert assurance_from_record(rec_false).verifier_account_isolated is False

    rec_sb_true = make_record({"sandbox": True})
    assert assurance_from_record(rec_sb_true).verifier_account_isolated is True

    rec_sb_false = make_record({"sandbox": False})
    assert assurance_from_record(rec_sb_false).verifier_account_isolated is False


def test_assurance_from_record_reads_verifier_account_from_observations() -> None:
    rec_obs_va = make_record(
        {
            "evidence_bundle": {
                "observations": [
                    {"verifier_id": "pytest", "verifier_account": True},
                ]
            }
        }
    )
    assert assurance_from_record(rec_obs_va).verifier_account_isolated is True

    rec_obs_sb = make_record(
        {
            "evidence_bundle": {
                "observations": [
                    {"verifier_id": "pytest", "sandbox": True},
                ]
            }
        }
    )
    assert assurance_from_record(rec_obs_sb).verifier_account_isolated is True

    rec_obs_isolated = make_record(
        {
            "evidence_bundle": {
                "observations": [
                    {"verifier_id": "pytest", "verifier_account_isolated": True},
                ]
            }
        }
    )
    assert assurance_from_record(rec_obs_isolated).verifier_account_isolated is True


def test_assurance_sandbox_backend_allowed_and_unknown() -> None:
    record = make_record()
    for backend in ("seatbelt", "bubblewrap", "none", "unknown"):
        assurance = assurance_from_record(record, run_meta={"sandbox_backend": backend})
        assert assurance.sandbox_backend == backend

    invalid = assurance_from_record(record, run_meta={"sandbox_backend": "docker"})
    assert invalid.sandbox_backend == "unknown"


def test_assurance_network_mode() -> None:
    record = make_record()
    for mode in ("off", "on", "unknown"):
        assurance = assurance_from_record(record, run_meta={"network": mode})
        assert assurance.network == mode

    unrecognised = assurance_from_record(record, run_meta={"network": "bridged"})
    assert unrecognised.network == "unknown"


def test_assurance_worker_isolated_not_assumed_false() -> None:
    record = make_record()
    explicit_true = assurance_from_record(record, run_meta={"worker_isolated": True})
    assert explicit_true.worker_isolated is True

    explicit_false = assurance_from_record(record, run_meta={"worker_isolated": False})
    assert explicit_false.worker_isolated is False

    not_specified = assurance_from_record(record, run_meta={})
    assert not_specified.worker_isolated is None

    no_meta = assurance_from_record(record, run_meta=None)
    assert no_meta.worker_isolated is None


def test_assurance_hidden_tests_requires_test_commit_and_record_evidence() -> None:
    plain_record = make_record({"acceptance_contract": {"allowed_paths": ["src/a.py"]}})
    unsupported = assurance_from_record(plain_record, run_meta={"test_commit": "a" * 40})
    assert unsupported.hidden_tests is None

    contract_with_tests = {
        "acceptance_contract": {
            "allowed_paths": ["src/a.py"],
            "test_files": ["tests/test_hidden.py"],
        },
        "evidence_bundle": {
            "observations": [
                {
                    "verifier_id": "pytest",
                    "artifact_id": "tests/test_hidden.py",
                    "status": "PASS",
                }
            ]
        },
    }
    applied_record = make_record(contract_with_tests)
    supported = assurance_from_record(applied_record, run_meta={"test_commit": "a" * 40})
    assert supported.hidden_tests is True

    empty_commit = assurance_from_record(applied_record, run_meta={"test_commit": ""})
    assert empty_commit.hidden_tests is None

    no_meta = assurance_from_record(applied_record, run_meta=None)
    assert no_meta.hidden_tests is None


def test_asserted_by_caller_appears_iff_run_meta_used() -> None:
    record = make_record()

    no_meta = assurance_from_record(record, run_meta=None)
    assert no_meta.asserted_by_caller is False
    assert "asserted_by_caller" not in no_meta.to_dict()

    empty_meta = assurance_from_record(record, run_meta={})
    assert empty_meta.asserted_by_caller is False
    assert "asserted_by_caller" not in empty_meta.to_dict()

    with_backend = assurance_from_record(record, run_meta={"sandbox_backend": "seatbelt"})
    assert with_backend.asserted_by_caller is True
    assert with_backend.to_dict().get("asserted_by_caller") is True

    with_net = assurance_from_record(record, run_meta={"network": "off"})
    assert with_net.asserted_by_caller is True
    assert with_net.to_dict().get("asserted_by_caller") is True

    with_worker = assurance_from_record(record, run_meta={"worker_isolated": True})
    assert with_worker.asserted_by_caller is True
    assert with_worker.to_dict().get("asserted_by_caller") is True

    with_test_commit = assurance_from_record(record, run_meta={"test_commit": "a" * 40})
    assert with_test_commit.asserted_by_caller is True
    assert with_test_commit.to_dict().get("asserted_by_caller") is True


def test_to_dict_renders_unknown() -> None:
    default_assurance = Assurance()
    rendered = default_assurance.to_dict()

    assert rendered["sandbox_backend"] == "unknown"
    assert rendered["verifier_account_isolated"] == "unknown"
    assert rendered["worker_isolated"] == "unknown"
    assert rendered["hidden_tests"] == "unknown"
    assert rendered["network"] == "unknown"
    assert "asserted_by_caller" not in rendered

    configured = Assurance(
        sandbox_backend="seatbelt",
        verifier_account_isolated=True,
        worker_isolated=False,
        hidden_tests=None,
        network="off",
        asserted_by_caller=True,
    )
    rendered_configured = configured.to_dict()
    assert rendered_configured["sandbox_backend"] == "seatbelt"
    assert rendered_configured["verifier_account_isolated"] is True
    assert rendered_configured["worker_isolated"] is False
    assert rendered_configured["hidden_tests"] == "unknown"
    assert rendered_configured["network"] == "off"
    assert rendered_configured["asserted_by_caller"] is True
