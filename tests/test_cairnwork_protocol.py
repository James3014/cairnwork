"""cairnwork_protocol against Nexus Core's published protocol: the descriptor's
conformance vectors byte for byte, a differential check against Core's own functions,
and drift detection on the constants and canonicalization rules."""

from __future__ import annotations

import pytest
from core_http_server import core_http_server
from hypothesis import given, settings
from hypothesis import strategies as st
from product import protocol as core_protocol
from product.protocol import generic_verification as core

import cairnwork_protocol as ours
from cairnwork_protocol.http import CoreHttpError, check_core_url, http_core_verify

DESCRIPTOR = core.protocol_descriptor()
HASHERS = {
    "acceptance_contract": ours.acceptance_contract_hash,
    "change_set": ours.change_set_hash,
    "verification_plan": ours.verification_plan_hash,
    "evidence_bundle": ours.evidence_bundle_hash,
    "change_manifest": ours.change_manifest_hash,
}


def _hasher(name: str):
    return next(fn for prefix, fn in HASHERS.items() if name.startswith(prefix))


@pytest.mark.parametrize("name", sorted(DESCRIPTOR["conformance_vectors"]))
def test_conformance_vectors_hash_byte_for_byte(name):
    vector = DESCRIPTOR["conformance_vectors"][name]
    assert _hasher(name)(vector["value"]) == vector["expected_hash"]


def test_ordering_vectors():
    vectors = DESCRIPTOR["ordering_conformance_vectors"]
    fields = vectors["json_object_field_order"]
    for side in (fields["left"], fields["right"]):
        assert ours.canonical_json(side) == fields["expected_canonical_json"]
        assert ours.canonical_hash(side) == fields["expected_hash"]
    contracts = vectors["acceptance_path_and_verifier_order"]
    assert ours.acceptance_contract_hash(contracts["left"]) == ours.acceptance_contract_hash(
        contracts["right"]
    )
    bundles = vectors["evidence_observation_order"]
    assert ours.evidence_bundle_hash(bundles["left"]) == ours.evidence_bundle_hash(bundles["right"])


def test_the_request_vector_binds_with_our_hashes():
    request = DESCRIPTOR["request_conformance_vector"]
    plan, evidence = request["verification_plan"], request["evidence_bundle"]
    contract_hash = ours.acceptance_contract_hash(request["acceptance_contract"])
    change_hash = ours.change_set_hash(request["change_set"])
    assert plan["acceptance_contract_hash"] == contract_hash == evidence["acceptance_contract_hash"]
    assert plan["change_set_hash"] == change_hash == evidence["change_set_hash"]
    assert evidence["verification_plan_hash"] == ours.verification_plan_hash(plan)
    assert request["change_set"]["diff_hash"] == ours.change_manifest_hash(
        request["change_manifest"]
    )


def test_constants_and_rules_have_not_drifted():
    assert ours.PUBLIC_PROTOCOL_VERSION == core_protocol.PUBLIC_PROTOCOL_VERSION
    schema = core.GENERIC_VERIFICATION_REQUEST_SCHEMA_ID
    assert ours.GENERIC_VERIFICATION_REQUEST_SCHEMA_ID == schema
    assert ours.CHANGE_MANIFEST_SCHEMA_ID == core.CHANGE_MANIFEST_SCHEMA_ID
    rules = DESCRIPTOR["canonicalization"]
    assert (rules["encoding"], rules["ensure_ascii"], rules["allow_nan"]) == ("utf-8", True, False)
    assert rules["json_separators"] == [",", ":"]
    assert rules["json_object_keys"] == "lexicographic"


TEXT = st.text(min_size=1, max_size=12)
HASH = st.from_regex(r"sha256:[0-9a-f]{64}", fullmatch=True)
OID = st.from_regex(r"[0-9a-f]{40}", fullmatch=True)
NAMES = st.lists(TEXT, max_size=4, unique=True)
SUBJECT = st.fixed_dictionaries(
    {"logical_subject_id": TEXT, "evidence_kind": TEXT, "requirement_mode": TEXT,
     "applicability": TEXT}
)  # fmt: skip
SUBJECTS = st.lists(SUBJECT, max_size=3, unique_by=lambda s: s["logical_subject_id"])
CONTRACT = st.fixed_dictionaries(
    {"contract_id": TEXT, "requirements_hash": HASH, "required_verifier_ids": NAMES,
     "allowed_paths": NAMES, "deletion_policy": st.sampled_from(["ALLOW", "FORBID"])},
    optional={"expected_subjects": SUBJECTS, "universe_generation": st.integers(0, 9),
              "allowed_path_prefixes": NAMES},
)  # fmt: skip
CHANGE_SET = st.fixed_dictionaries(
    {"change_set_id": TEXT, "source_revision": TEXT, "target_revision": TEXT, "diff_hash": HASH,
     "paths": NAMES, "deleted_paths": NAMES}
)  # fmt: skip
ROW = {"verifier_id": TEXT, "artifact_id": TEXT, "artifact_hash": HASH, "status": TEXT}
OBSERVATION = st.fixed_dictionaries(ROW) | st.fixed_dictionaries(
    {**ROW, "logical_subject_id": TEXT, "evidence_kind": TEXT}  # logical identity comes as a pair
)
BUNDLE = st.fixed_dictionaries(
    {"bundle_id": TEXT, "acceptance_contract_hash": HASH, "change_set_hash": HASH,
     "verification_plan_hash": HASH,
     "observations": st.lists(OBSERVATION, max_size=4,
                              unique_by=lambda r: (r["verifier_id"], r["artifact_id"]))}
)  # fmt: skip
ENTRY = st.fixed_dictionaries(
    {"path": TEXT, "change_type": st.sampled_from(["ADD", "MODIFY", "DELETE"]),
     "before_oid": st.none() | OID, "after_oid": st.none() | OID,
     "before_mode": st.none() | st.just("100644"), "after_mode": st.none() | st.just("100644")}
)  # fmt: skip
MANIFEST = st.fixed_dictionaries(
    {"source_tree": TEXT, "target_tree": TEXT,
     "entries": st.lists(ENTRY, max_size=4, unique_by=lambda r: r["path"])}
)  # fmt: skip


@settings(max_examples=150, deadline=None)
@given(CONTRACT, CHANGE_SET, BUNDLE, MANIFEST)
def test_same_hashes_as_core_for_any_input(contract, change_set, bundle, manifest):
    assert ours.acceptance_contract_hash(contract) == core.acceptance_contract_hash(contract)
    assert ours.change_set_hash(change_set) == core.change_set_hash(change_set)
    assert ours.evidence_bundle_hash(bundle) == core.evidence_bundle_hash(bundle)
    assert ours.change_manifest_hash(manifest) == core.change_manifest_hash(manifest)
    plan = {"plan_id": contract["contract_id"],
            "acceptance_contract_hash": bundle["acceptance_contract_hash"],
            "change_set_hash": bundle["change_set_hash"],
            "required_verifier_ids": contract["required_verifier_ids"]}  # fmt: skip
    assert ours.verification_plan_hash(plan) == core.verification_plan_hash(plan)


@pytest.mark.parametrize(
    "url",
    ["http://10.0.0.5:8080", "http://core.example.com", "ftp://127.0.0.1", "127.0.0.1:80",
     "http://127.0.0.1:8080/v1", "http://user@127.0.0.1:8080", "https://"],
)  # fmt: skip
def test_core_url_must_be_a_loopback_http_or_an_https_root(url):
    with pytest.raises(CoreHttpError):
        check_core_url(url)


@pytest.mark.parametrize(
    ("url", "root"),
    [("http://127.0.0.1:8080/", "http://127.0.0.1:8080"), ("http://[::1]:9", "http://[::1]:9"),
     ("https://core.example.com", "https://core.example.com")],
)  # fmt: skip
def test_accepted_core_urls(url, root):
    assert check_core_url(url) == root


def test_a_wrong_token_returns_core_s_refusal_not_an_exception(tmp_path):
    with core_http_server(tmp_path) as (url, token_file):
        token_file.write_text("wrong")
        status, body = http_core_verify(url, token_file)(DESCRIPTOR["request_conformance_vector"])
    assert status == 401 and body["error"]["code"] == "UNAUTHORIZED"


def test_an_empty_token_is_refused_before_calling(tmp_path):
    with core_http_server(tmp_path) as (url, token_file):
        token_file.write_text("\n")
        with pytest.raises(CoreHttpError):
            http_core_verify(url, token_file)({})


def test_the_request_vector_gets_the_same_answer_over_http(tmp_path):
    request = DESCRIPTOR["request_conformance_vector"]
    from product.adapters.generic_verification import verify_generic_changeset

    with core_http_server(tmp_path) as (url, token_file):
        assert http_core_verify(url, token_file)(request) == verify_generic_changeset(request)
