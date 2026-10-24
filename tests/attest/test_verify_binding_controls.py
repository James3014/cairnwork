"""#229 negative controls against a consistent lying signer: the statement is edited, then its
request hash and Core's response hashes are recomputed and it is re-signed with the producer's
own key, so the only check left to catch each edit is the one its test names (removing that
check makes the receipt verify OK)."""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

import pytest

from cairnwork_attest.bundle import read_bundle
from cairnwork_attest.verify import verify_bundle
from cairnwork_protocol import change_manifest_hash
from tests.attest.binding_helpers import (
    commit_on,
    git,
    notarize_bound,
    policy,
    producer,
    receiver,
    resign,
    sign_bound,
)
from tests.attest.test_verify import fake_core_verify


@pytest.fixture
def world(tmp_path: Path) -> dict:
    p = producer(tmp_path)
    state = tmp_path / "state"
    run = notarize_bound(state, p)
    bundle = sign_bound(state, run, tmp_path / "bundle.json")
    full = receiver(tmp_path, "full", p["work"], p["head"], p["tests"])
    conf = receiver(tmp_path, "conf", p["work"], p["head"])
    return {**p, "state": state, "run": run, "bundle": bundle, "full": full, "conf": conf,
            "pub": state / "public.pem", "tmp": tmp_path}  # fmt: skip


def _statement(bundle: Path) -> dict:
    return json.loads(base64.b64decode(read_bundle(bundle)["payload"]))


def _liar(w: dict, stmt: dict, name: str) -> Path:
    """Re-sign stmt with every hash recomputed from its (edited) request."""
    predicate = stmt["predicate"]
    predicate["response"]["hashes"] = fake_core_verify(predicate["request"])[1]["hashes"]
    return resign(w["state"], stmt, w["tmp"] / f"{name}.json", rehash=True)


def _check(w: dict, bundle: Path, repo: str = "full", **kw):
    kw.setdefault("expected_candidate", w["head"])
    if repo == "conf":
        kw.setdefault("policy", policy(w["state"], w["tmp"], w["tests"]))
    return verify_bundle(bundle, public_key_pem=w["pub"], repo=w[repo], **kw)


def test_the_unedited_receipt_verifies_in_both_modes(world):
    assert _check(world, _liar(world, _statement(world["bundle"]), "same")).ok
    assert _check(world, _liar(world, _statement(world["bundle"]), "same2"), repo="conf").ok


def test_m10_a_subject_other_than_the_binding_head_is_head_mismatch(world):
    stmt = _statement(world["bundle"])
    stmt["subject"][0]["digest"]["gitCommit"] = world["other"]
    result = _check(world, _liar(world, stmt, "m10"))  # --candidate is the binding's head
    assert result.code == "HEAD_MISMATCH" and f"subject {world['other']}" in result.detail


def test_m20_a_predicate_base_other_than_the_binding_base_is_base_mismatch(world):
    stmt = _statement(world["bundle"])
    stmt["predicate"]["base_commit"] = world["other"]
    result = _check(world, _liar(world, stmt, "m20"))
    assert result.code == "BASE_MISMATCH" and "predicate.base_commit" in result.detail


def test_m2_a_request_from_another_source_revision_is_binding_hash_mismatch(world):
    stmt = _statement(world["bundle"])
    stmt["predicate"]["request"]["change_set"]["source_revision"] = f"git-commit:{world['other']}"
    result = _check(world, _liar(world, stmt, "m2"))
    assert result.code == "BINDING_HASH_MISMATCH" and "source_revision" in result.detail


def test_m16_a_manifest_for_another_target_tree_is_binding_hash_mismatch(world):
    """Confidential mode, where nothing rebuilds the manifest to catch it later."""
    stmt = _statement(world["bundle"])
    request = stmt["predicate"]["request"]
    request["change_manifest"]["target_tree"] = "git-tree:" + git(
        world["work"], "rev-parse", f"{world['other']}^{{tree}}"
    )
    request["change_set"]["diff_hash"] = change_manifest_hash(request["change_manifest"])
    result = _check(world, _liar(world, stmt, "m16"), repo="conf")
    assert result.code == "BINDING_HASH_MISMATCH" and "target_tree" in result.detail


def test_m4_a_diff_hash_that_is_not_the_manifests_is_tree_mismatch(world):
    stmt = _statement(world["bundle"])
    stmt["predicate"]["request"]["change_set"]["diff_hash"] = "sha256:" + "0" * 64
    result = _check(world, _liar(world, stmt, "m4"))
    assert result.code == "TREE_MISMATCH" and "diff_hash" in result.detail


def test_m8_a_manifest_from_another_source_tree_is_head_tree_mismatch(world, tmp_path):
    """The tests ran over H' = head + src/b.py. The signer claims head H and hides src/b.py by
    starting Core's manifest at S = base + src/b.py instead of the base tree."""
    work, base = world["work"], world["base"]
    b_py = {"src/b.py": "import os\n"}
    h_prime = commit_on(work, base, {"src/a.py": "x = 2\n", **b_py}, "head plus b")
    s_tree = git(work, "rev-parse", commit_on(work, base, b_py, "only b") + "^{tree}")
    run = notarize_bound(world["state"], world, attempt="m8", overlay_head=h_prime)
    stmt = _statement(sign_bound(world["state"], run, tmp_path / "m8-signed.json"))
    manifest = stmt["predicate"]["request"]["change_manifest"]
    manifest["source_tree"] = f"git-tree:{s_tree}"
    manifest["entries"] = [e for e in manifest["entries"] if e["path"] != "src/b.py"]
    stmt["predicate"]["request"]["change_set"]["diff_hash"] = change_manifest_hash(manifest)
    result = _check(world, _liar(world, stmt, "m8"), repo="conf")
    assert result.code == "HEAD_TREE_MISMATCH" and "does not start at the base" in result.detail


def test_a_manifest_entry_whose_path_is_not_a_string_is_head_tree_mismatch(world):
    stmt = _statement(world["bundle"])
    stmt["predicate"]["request"]["change_manifest"]["entries"][0]["path"] = []
    out = resign(world["state"], stmt, world["tmp"] / "path.json", rehash=True)
    result = _check(world, out, repo="conf")  # was a TypeError
    assert result.code == "HEAD_TREE_MISMATCH" and "malformed" in result.detail


@pytest.mark.parametrize(
    "edit",
    [
        lambda p: p.pop("test_commit"),
        lambda p: p.update(core_identity=None),
        lambda p: p.update(key_id=None),
    ],
)
def test_a_policy_dict_of_the_wrong_shape_is_policy_malformed(world, edit):
    """verify_bundle's own callers may pass a dict that never went through load_policy."""
    pol = policy(world["state"], world["tmp"], world["tests"])
    edit(pol)
    for repo in ("full", "conf"):
        assert _check(world, world["bundle"], repo=repo, policy=pol).code == "POLICY_MALFORMED"


def test_a_receiver_that_cannot_write_objects_is_rebuild_failed_not_tree_mismatch(world):
    objects = world["full"] / ".git" / "objects"
    subprocess.run(["chmod", "-R", "a-w", str(objects)], check=True)
    try:
        result = _check(world, world["bundle"])
    finally:
        subprocess.run(["chmod", "-R", "u+w", str(objects)], check=True)
    assert result.code == "REBUILD_FAILED", result
    assert _check(world, world["bundle"]).ok  # the same receipt, once the repo is writable
