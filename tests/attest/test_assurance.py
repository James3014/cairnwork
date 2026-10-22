"""Tests for cairnwork_attest.assurance."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from conftest import commit_change, git

from cairnwork.core_request import AcceptanceSpec, VerifierObservation
from cairnwork.notary import Notary, default_core_identity
from cairnwork_attest.assurance import (
    ALLOWED_NETWORK_MODES,
    ALLOWED_SANDBOX_BACKENDS,
    Assurance,
    AssuranceConfigError,
    assurance_from_record,
)
from cairnwork_attest.record import verify_record
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


def test_assurance_from_record_renders_unknown_for_real_record(tmp_path: Path, repo: Path) -> None:
    db_path = tmp_path / "notary.sqlite"
    _, verdict = setup_sample_notary(db_path, repo, "task-1", "a1")
    verified = verify_record(db_path, "task-1", "a1", expected_candidate=verdict.candidate_commit)

    assurance = assurance_from_record(verified)
    assert assurance.sandbox_backend == "unknown"
    assert assurance.verifier_account_isolated is None
    assert assurance.worker_isolated is None
    assert assurance.hidden_tests is None
    assert assurance.network == "unknown"
    assert not hasattr(assurance, "asserted_by_caller")

    rendered = assurance.to_dict()
    assert rendered == {
        "sandbox_backend": "unknown",
        "verifier_account_isolated": "unknown",
        "worker_isolated": "unknown",
        "hidden_tests": "unknown",
        "network": "unknown",
    }
    assert set(rendered.keys()) == {
        "sandbox_backend",
        "verifier_account_isolated",
        "worker_isolated",
        "hidden_tests",
        "network",
    }
    assert len(rendered) == 5
    assert "asserted_by_caller" not in rendered


def test_assurance_dataclass_defaults_and_explicit_values() -> None:
    default_a = Assurance()
    rendered_default = default_a.to_dict()
    assert rendered_default == {
        "sandbox_backend": "unknown",
        "verifier_account_isolated": "unknown",
        "worker_isolated": "unknown",
        "hidden_tests": "unknown",
        "network": "unknown",
    }
    assert set(rendered_default.keys()) == {
        "sandbox_backend",
        "verifier_account_isolated",
        "worker_isolated",
        "hidden_tests",
        "network",
    }

    custom = Assurance(
        sandbox_backend="seatbelt",
        verifier_account_isolated=True,
        worker_isolated=False,
        hidden_tests=True,
        network="off",
    )
    rendered_custom = custom.to_dict()
    assert rendered_custom == {
        "sandbox_backend": "seatbelt",
        "verifier_account_isolated": True,
        "worker_isolated": False,
        "hidden_tests": True,
        "network": "off",
    }
    assert set(rendered_custom.keys()) == {
        "sandbox_backend",
        "verifier_account_isolated",
        "worker_isolated",
        "hidden_tests",
        "network",
    }
    assert len(rendered_custom) == 5


def test_assurance_from_record_rejects_non_verified_record() -> None:
    caller: Any = assurance_from_record
    with pytest.raises(AssuranceConfigError) as exc_info:
        caller("not-a-verified-record")
    assert exc_info.value.code == "ASSURANCE_CONFIG_ERROR"

    with pytest.raises(AssuranceConfigError):
        caller(None)


def test_allowed_backends_and_modes() -> None:
    assert "seatbelt" in ALLOWED_SANDBOX_BACKENDS
    assert "bubblewrap" in ALLOWED_SANDBOX_BACKENDS
    assert "none" in ALLOWED_SANDBOX_BACKENDS
    assert "unknown" in ALLOWED_SANDBOX_BACKENDS

    assert "off" in ALLOWED_NETWORK_MODES
    assert "on" in ALLOWED_NETWORK_MODES
    assert "unknown" in ALLOWED_NETWORK_MODES
