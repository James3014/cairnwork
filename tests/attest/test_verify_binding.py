"""Receiver side of #229: a v0.2 receipt verified offline, in full and confidential mode, and
every negative control with the code that names which side does not match."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from cairnwork_attest.bundle import read_bundle, write_bundle
from cairnwork_attest.cli import main
from cairnwork_attest.verify import verify_bundle
from cairnwork_attest.verify_binding import PolicyError, load_policy
from tests.attest.binding_helpers import (
    notarize_bound,
    policy,
    producer,
    receiver,
    resign,
    sign_bound,
)


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


def check(w: dict, bundle: Path | None = None, repo: str = "full", **kw):
    kw.setdefault("expected_candidate", w["head"])
    return verify_bundle(bundle or w["bundle"], public_key_pem=w["pub"], repo=w[repo], **kw)


def statement(w: dict) -> dict:
    payload = read_bundle(w["bundle"])["payload"]
    return json.loads(base64.b64decode(payload))


def test_full_mode_rebuilds_the_executed_tree_without_the_scratch_commit(world):
    from cairnwork.gitcmd import GitCommandError, run_git

    with pytest.raises(GitCommandError):  # the receiver never had the commit that ran
        run_git(world["full"], "cat-file", "-e", world["run"]["judged"])
    result = check(world)
    assert result.ok and result.assurance is not None and result.statement is not None, result
    assert result.assurance["mode"] == "full"
    assert result.assurance["test_contents"] == "verified"
    assert result.assurance["test_source"] == "signer_choice"  # no policy: not pinned
    stmt = result.statement
    assert stmt["subject"][0]["digest"]["gitCommit"] == world["head"]
    assert stmt["predicate"]["hidden_test_commit"] == world["tests"]
    assert stmt["predicateType"].endswith("/v0.2")


def test_confidential_mode_needs_a_policy_and_says_what_it_did_not_check(world):
    assert check(world, repo="conf").code == "POLICY_REQUIRED"
    pol = policy(world["state"], world["tmp"], world["tests"])
    result = check(world, repo="conf", policy=pol)
    assert result.ok and result.assurance is not None, result
    assert result.assurance["mode"] == "confidential"
    assert result.assurance["test_contents"] == "not_verified"
    assert result.assurance["overlay"] == "not_verified"
    assert result.assurance["test_source"] == "policy_pinned"


def test_a_policy_is_also_checked_in_full_mode(world):
    pol = policy(world["state"], world["tmp"], world["tests"])
    assert check(world, policy=pol).ok
    other = policy(world["state"], world["tmp"], "d" * 40)
    assert check(world, policy=other).code == "TEST_SOURCE_MISMATCH"


@pytest.mark.parametrize("repo", ["full", "conf"])
def test_a_policy_pinning_other_tests_is_test_source_mismatch(world, repo):
    pol = policy(world["state"], world["tmp"], "d" * 40)
    assert check(world, repo=repo, policy=pol).code == "TEST_SOURCE_MISMATCH"


def test_a_policy_pinning_another_key_or_core_is_refused(world):
    pol = policy(world["state"], world["tmp"], world["tests"], key_id="f" * 64)
    assert check(world, repo="conf", policy=pol).code == "SIGNATURE_INVALID"
    pol = policy(world["state"], world["tmp"], world["tests"], core_identity={"version": "9"})
    assert check(world, repo="conf", policy=pol).code == "CORE_IDENTITY_MISMATCH"


def test_another_head_is_head_mismatch(world):
    assert check(world, expected_candidate=world["other"]).code == "HEAD_MISMATCH"


def test_another_base_and_a_missing_base(world, tmp_path):
    assert check(world, expected_base="1" * 40).code == "BASE_MISMATCH"
    shallow = receiver(tmp_path, "shallow", world["work"], world["head"], depth=1)
    pol = policy(world["state"], world["tmp"], world["tests"])
    result = verify_bundle(world["bundle"], public_key_pem=world["pub"], repo=shallow,
                           expected_candidate=world["head"], policy=pol)  # fmt: skip
    assert result.code == "BASE_NOT_FOUND"


def test_binding_of_run_a_with_request_of_run_b_is_binding_hash_mismatch(world, tmp_path):
    run_b = notarize_bound(world["state"], world, attempt="a2", head=world["other"])
    bundle_b = sign_bound(world["state"], run_b, tmp_path / "b.json")
    mixed = statement(world)
    b = json.loads(base64.b64decode(read_bundle(bundle_b)["payload"]))
    for key in ("request", "response", "request_hash"):
        mixed["predicate"][key] = b["predicate"][key]
    out = resign(world["state"], mixed, tmp_path / "mixed.json")
    assert check(world, out).code == "BINDING_HASH_MISMATCH"
    swapped = statement(world)
    swapped["predicate"]["binding"] = b["predicate"]["binding"]
    out = resign(world["state"], swapped, tmp_path / "swapped.json")
    assert check(world, out, expected_candidate=world["other"]).code == "BINDING_HASH_MISMATCH"


def test_binding_text_edited_after_signing(world, tmp_path):
    envelope = read_bundle(world["bundle"])
    stmt = statement(world)
    stmt["predicate"]["binding"]["test_paths"] = ["tests/test_new.py"]
    raw = json.dumps(stmt, sort_keys=True, separators=(",", ":")).encode()
    write_bundle(tmp_path / "edited.json", {**envelope, "payload": base64.b64encode(raw).decode()})
    assert check(world, tmp_path / "edited.json").code == "SIGNATURE_INVALID"
    out = resign(world["state"], stmt, tmp_path / "resigned.json")  # the signer's own mistake
    assert check(world, out).code == "BINDING_HASH_MISMATCH"


def test_subject_changed_without_resigning_is_signature_invalid(world, tmp_path):
    envelope = read_bundle(world["bundle"])
    stmt = statement(world)
    stmt["subject"][0]["digest"]["gitCommit"] = world["other"]
    raw = json.dumps(stmt, sort_keys=True, separators=(",", ":")).encode()
    write_bundle(tmp_path / "s.json", {**envelope, "payload": base64.b64encode(raw).decode()})
    assert check(world, tmp_path / "s.json", expected_candidate=world["other"]).code == (
        "SIGNATURE_INVALID"
    )


def test_a_v02_statement_without_binding_is_statement_shape(world, tmp_path):
    stmt = statement(world)
    del stmt["predicate"]["binding"]
    assert check(world, resign(world["state"], stmt, tmp_path / "x.json")).code == (
        "STATEMENT_SHAPE"
    )


def test_an_unknown_overlay_rule_is_refused(world, tmp_path):
    run = notarize_bound(world["state"], world, attempt="a3", rule="overlay-v9")
    out = sign_bound(world["state"], run, tmp_path / "v9.json")
    result = check(world, out)
    assert result.code == "STATEMENT_SHAPE" and "overlay-v9" in result.detail
    (tmp_path / "p.json").write_text(json.dumps({**policy(world["state"], tmp_path, world["tests"]),
                                                 "rule": "overlay-v9"}))  # fmt: skip
    with pytest.raises(PolicyError):
        load_policy(tmp_path / "p.json")


def test_a_head_swapped_over_another_heads_tree_is_tree_or_head_tree_mismatch(world, tmp_path):
    """The binding names head B, but the tree that ran is head A plus the tests."""
    run = notarize_bound(world["state"], world, attempt="a4", head=world["other"],
                         overlay_head=world["head"])  # fmt: skip
    out = sign_bound(world["state"], run, tmp_path / "swap.json")
    full = receiver(tmp_path, "full2", world["work"], world["other"], world["tests"])
    conf = receiver(tmp_path, "conf2", world["work"], world["other"])
    pol = policy(world["state"], world["tmp"], world["tests"])
    kw = {"public_key_pem": world["pub"], "expected_candidate": world["other"]}
    result = verify_bundle(out, repo=full, **kw)
    rebuilt = run["binding"]["executed_tree"]
    assert result.code == "TREE_MISMATCH" and "head + tests give" in result.detail
    assert rebuilt in result.detail  # names both sides: the rebuilt tree and the bound one
    result = verify_bundle(out, repo=conf, policy=pol, **kw)
    assert result.code == "HEAD_TREE_MISMATCH" and "src/a.py" in result.detail


def test_test_paths_that_are_not_the_test_commit_are_test_source_mismatch(world, tmp_path):
    run = notarize_bound(world["state"], world, attempt="a5", paths=("tests/test_new.py",))
    out = sign_bound(world["state"], run, tmp_path / "fewer.json")
    assert check(world, out).code == "TEST_SOURCE_MISMATCH"
    pol = policy(world["state"], world["tmp"], world["tests"])
    assert check(world, out, repo="conf", policy=pol).ok  # confidential: the signer's word


def test_a_v01_receipt_under_a_policy_is_test_source_mismatch(tmp_path, repo):
    from tests.attest.test_verify import setup_verified_bundle_fixture

    state = tmp_path / "s01"
    bundle, pub, _, cand = setup_verified_bundle_fixture(state, repo)
    pol = {"rule": "overlay-v1", "test_commit": "c" * 40, "core_identity": {}, "key_id": "0" * 64}
    kw = {"public_key_pem": pub, "repo": repo, "expected_candidate": cand}
    assert verify_bundle(bundle, **kw).ok
    assert verify_bundle(bundle, policy=pol, **kw).code == "TEST_SOURCE_MISMATCH"


def _break(world: dict, tmp_path: Path, faults: tuple[str, ...]) -> tuple[Path, dict]:
    """A statement (re-signed by the producer) and verify kwargs with several faults at once."""
    stmt, kw = statement(world), {"repo": "full"}
    if "shape" in faults:
        stmt["predicate"]["binding"]["rule"] = "overlay-v9"
    if "chain" in faults:
        stmt["predicate"]["binding"]["verifiers"] = [["pytest-0", ["true"]]]
    if "head" in faults:
        kw["expected_candidate"] = world["other"]
    if "base" in faults:
        kw["expected_base"] = "1" * 40
    if "test_source" in faults:
        stmt["predicate"]["hidden_test_commit"] = "2" * 40
    if "core" in faults:
        stmt["predicate"]["response"]["verification"]["status"] = "FAILED_VERIFICATION"
    if "policy" in faults:
        kw["repo"] = "conf"
    return resign(world["state"], stmt, tmp_path / f"{'-'.join(faults)}.json"), kw


@pytest.mark.parametrize(
    ("faults", "code"),
    [
        (("shape", "policy", "chain", "head", "core"), "STATEMENT_SHAPE"),
        (("policy", "chain", "head", "core"), "POLICY_REQUIRED"),
        (("chain", "head", "base", "test_source", "core"), "BINDING_HASH_MISMATCH"),
        (("head", "base", "test_source", "core"), "HEAD_MISMATCH"),
        (("base", "test_source", "core"), "BASE_MISMATCH"),
        (("test_source", "core"), "TEST_SOURCE_MISMATCH"),
        (("core",), "RESPONSE_NOT_VERIFIED"),
    ],
)
def test_several_faults_report_the_first_in_the_fixed_order(world, tmp_path, faults, code):
    out, kw = _break(world, tmp_path, faults)
    assert check(world, out, **kw).code == code


def test_tree_comes_before_the_core_verdict(world, tmp_path):
    run = notarize_bound(world["state"], world, attempt="a6", head=world["other"],
                         overlay_head=world["head"])  # fmt: skip
    out = sign_bound(world["state"], run, tmp_path / "t.json")
    stmt = json.loads(base64.b64decode(read_bundle(out)["payload"]))
    stmt["predicate"]["response"]["verification"]["status"] = "FAILED_VERIFICATION"
    out = resign(world["state"], stmt, tmp_path / "t2.json")
    full = receiver(tmp_path, "full3", world["work"], world["other"], world["tests"])
    result = verify_bundle(out, public_key_pem=world["pub"], repo=full,
                           expected_candidate=world["other"])  # fmt: skip
    assert result.code == "TREE_MISMATCH"


def test_the_cli_takes_a_policy_and_prints_the_assurance(world, capsys):
    policy(world["state"], world["tmp"], world["tests"])
    args = ["verify", str(world["bundle"]), "--public-key", str(world["pub"]), "--repo",
            str(world["conf"]), "--candidate", world["head"], "--json"]  # fmt: skip
    assert main(args) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "POLICY_REQUIRED"
    assert main([*args, "--policy", str(world["tmp"] / "policy.json")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["assurance"]["test_contents"] == "not_verified"
    (world["tmp"] / "bad.json").write_text("{}")
    assert main([*args, "--policy", str(world["tmp"] / "bad.json")]) == 2


def _edit_request(stmt: dict, what: str) -> None:
    request = stmt["predicate"]["request"]
    if what == "contract":
        request["acceptance_contract"]["allowed_paths"] = ["src/"]
    elif what == "target":
        request["change_set"]["target_revision"] = "git-tree:" + "3" * 40
    else:  # the manifest: drop head's own change
        manifest = request["change_manifest"]
        manifest["entries"] = [e for e in manifest["entries"] if e["path"] != "src/a.py"]


@pytest.mark.parametrize(
    ("what", "repo", "code"),
    [
        ("contract", "full", "BINDING_HASH_MISMATCH"),  # Core echoed another contract
        ("target", "full", "BINDING_HASH_MISMATCH"),  # the request is for another tree
        ("manifest", "full", "TREE_MISMATCH"),
        ("manifest", "conf", "HEAD_TREE_MISMATCH"),
    ],
)
def test_an_edited_request_is_caught_before_the_core_verdict(world, tmp_path, what, repo, code):
    stmt = statement(world)
    _edit_request(stmt, what)
    out = resign(world["state"], stmt, tmp_path / f"{what}.json", rehash=True)
    pol = policy(world["state"], world["tmp"], world["tests"]) if repo == "conf" else None
    assert check(world, out, repo=repo, policy=pol).code == code
