"""Build a nexus-certify generic verification request from a git change set.

Every hash is recomputed here with Core's own helpers; nothing is taken from
the worker. The acceptance contract comes from the task (allowed files and
required verifiers), the change set and manifest from git, and the evidence
from Cairnwork's own verifier runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cairnwork.changeset import GitChangeSet
from cairnwork_protocol import (
    GENERIC_VERIFICATION_REQUEST_SCHEMA_ID,
    PUBLIC_PROTOCOL_VERSION,
    acceptance_contract_hash,
    change_manifest_hash,
    change_set_hash,
    verification_plan_hash,
)


@dataclass(frozen=True)
class VerifierObservation:
    verifier_id: str
    artifact_id: str
    artifact_hash: str
    passed: bool


@dataclass(frozen=True)
class AcceptanceSpec:
    contract_id: str
    requirements_hash: str
    required_verifier_ids: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    allow_deletions: bool = False


def change_manifest(change: GitChangeSet) -> dict[str, Any]:
    return {
        "source_tree": f"git-tree:{change.base_tree}",
        "target_tree": f"git-tree:{change.candidate_tree}",
        "entries": [entry.to_core() for entry in change.entries],
    }


def build_core_request(
    spec: AcceptanceSpec,
    change: GitChangeSet,
    observations: tuple[VerifierObservation, ...],
    *,
    attempt_id: str,
) -> dict[str, Any]:
    manifest = change_manifest(change)
    contract = {
        "contract_id": spec.contract_id,
        "requirements_hash": spec.requirements_hash,
        "required_verifier_ids": sorted(spec.required_verifier_ids),
        "allowed_paths": sorted(spec.allowed_paths),
        "deletion_policy": "ALLOW" if spec.allow_deletions else "FORBID",
    }
    change_set = {
        "change_set_id": f"{spec.contract_id}/{attempt_id}",
        "source_revision": f"git-commit:{change.base_commit}",
        "target_revision": change.target_revision,
        "diff_hash": change_manifest_hash(manifest),
        "paths": change.paths,
        "deleted_paths": change.deleted_paths,
    }
    plan = {
        "plan_id": f"{spec.contract_id}/{attempt_id}/plan",
        "acceptance_contract_hash": acceptance_contract_hash(contract),
        "change_set_hash": change_set_hash(change_set),
        "required_verifier_ids": sorted(spec.required_verifier_ids),
    }
    evidence = {
        "bundle_id": f"{spec.contract_id}/{attempt_id}/evidence",
        "acceptance_contract_hash": plan["acceptance_contract_hash"],
        "change_set_hash": plan["change_set_hash"],
        "verification_plan_hash": verification_plan_hash(plan),
        "observations": [
            {
                "verifier_id": obs.verifier_id,
                "artifact_id": obs.artifact_id,
                "artifact_hash": obs.artifact_hash,
                "status": "PASS" if obs.passed else "FAIL",
            }
            for obs in observations
        ],
        "claimed_bundle_hash": None,
    }
    return {
        "protocol_version": PUBLIC_PROTOCOL_VERSION,
        "schema": GENERIC_VERIFICATION_REQUEST_SCHEMA_ID,
        "acceptance_contract": contract,
        "change_set": change_set,
        "change_manifest": manifest,
        "verification_plan": plan,
        "evidence_bundle": evidence,
        "certification_policy": None,
    }
