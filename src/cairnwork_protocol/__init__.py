"""The Nexus Core generic-verification wire protocol, implemented from its written rules.

Cairnwork builds and checks Core requests with this package instead of importing
nexus-certify's Python modules, so the protocol, not Core's code, is the contract.
The rules (canonical JSON, the per-object canonical values, schema ids) follow
``product.protocol.generic_verification`` in nexus-core, which declares itself
transport-neutral and publishes conformance vectors so that other implementations can
check themselves byte for byte; tests/test_cairnwork_protocol.py does exactly that.

This is protocol code, kept outside the Cairnwork core (src/cairnwork) like the
nexus-certify dependency it replaces.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

PUBLIC_PROTOCOL_VERSION = "0.1.0-experimental"
GENERIC_VERIFICATION_REQUEST_SCHEMA_ID = "nexus.core.generic-verification-request.v1-experimental"
CHANGE_MANIFEST_SCHEMA_ID = "nexus.core.git-change-manifest.v1-experimental"


def canonical_json(value: Any) -> str:
    """JSON-compatible values only, sorted string keys, no whitespace, ASCII, no NaN."""

    def normalize(item: Any) -> Any:
        if item is None or type(item) in (str, int, bool):
            return item
        if type(item) in (list, tuple):
            return [normalize(child) for child in item]
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise TypeError("canonical object keys must be strings")
            return {key: normalize(item[key]) for key in sorted(item)}
        raise TypeError(f"unsupported canonical value: {type(item).__name__}")

    return json.dumps(
        normalize(value), ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def canonical_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def acceptance_contract_hash(value: Mapping[str, Any]) -> str:
    result: list[Any] = [
        value["contract_id"],
        value["requirements_hash"],
        sorted(value["required_verifier_ids"]),
        sorted(value["allowed_paths"]),
        value["deletion_policy"],
    ]
    if value.get("expected_subjects"):  # a declared universe: generation, then subject rows
        subjects = sorted(value["expected_subjects"], key=lambda s: s["logical_subject_id"])
        result.append(value.get("universe_generation", 0))
        result.append(
            [
                [
                    s["logical_subject_id"],
                    s["evidence_kind"],
                    s["requirement_mode"],
                    s["applicability"],
                ]
                for s in subjects
            ]
        )
    if value.get("allowed_path_prefixes"):
        result.append(["allowed_path_prefixes", sorted(value["allowed_path_prefixes"])])
    return canonical_hash(result)


def change_set_hash(value: Mapping[str, Any]) -> str:
    result: list[Any] = [
        value["change_set_id"],
        value["source_revision"],
        value["target_revision"],
        value["diff_hash"],
        sorted(value["paths"]),
    ]
    if value["deleted_paths"]:  # only deletion-bearing change sets bind the deletion set
        result.append(sorted(value["deleted_paths"]))
    return canonical_hash(result)


def verification_plan_hash(value: Mapping[str, Any]) -> str:
    return canonical_hash(
        [
            value["plan_id"],
            value["acceptance_contract_hash"],
            value["change_set_hash"],
            sorted(value["required_verifier_ids"]),
        ]
    )


def evidence_bundle_hash(value: Mapping[str, Any]) -> str:
    def row(item: Mapping[str, Any]) -> list[Any]:
        fields = [item["verifier_id"], item["artifact_id"], item["artifact_hash"], item["status"]]
        if item.get("logical_subject_id") is not None:
            fields += [item["logical_subject_id"], item["evidence_kind"]]
        return fields

    observations = sorted(value["observations"], key=lambda r: (r["verifier_id"], r["artifact_id"]))
    return canonical_hash(
        [
            value["bundle_id"],
            value["acceptance_contract_hash"],
            value["change_set_hash"],
            value["verification_plan_hash"],
            [row(item) for item in observations],
        ]
    )


def change_manifest_hash(value: Mapping[str, Any]) -> str:
    entries = sorted(value["entries"], key=lambda r: r["path"])
    rows = [
        [
            r["path"],
            r["change_type"],
            r["before_oid"],
            r["after_oid"],
            r["before_mode"],
            r["after_mode"],
        ]
        for r in entries
    ]
    return canonical_hash(
        [CHANGE_MANIFEST_SCHEMA_ID, value["source_tree"], value["target_tree"], rows]
    )
