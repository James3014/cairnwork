"""#229 end to end: ``cairnwork-verify`` on an external candidate with deferred tests, the
scratch and run directories deleted, then ``cairnwork-attest verify --candidate <head>`` in a
fresh clone that holds only head, base and (full mode) the test commit."""

import json
import shutil
import subprocess
import sys

import pytest
from conftest import git
from test_deferred_tests import CHECK, REGRESSION, make_test_commit
from verify_helpers import FIX, base_of, candidate, verify

from cairnwork.notary import default_core_identity
from cairnwork_attest.keys import keyid, load_private_key, public_key_pem

sandbox = pytest.mark.sandbox
VERIFIER = " ".join(CHECK[0][1])


def attest(*args):
    done = subprocess.run(
        [sys.executable, "-I", "-m", "cairnwork_attest.cli", *map(str, args)],
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    return done.returncode, done.stdout, done.stderr


def signed(state, out, tmp_path, cand):
    assert attest("keygen", "--state", state)[0] == 0
    pub = tmp_path / "pub.pem"
    pub.write_text(public_key_pem(load_private_key(state).public_key()))
    bundle = tmp_path / "bundle.json"
    code, _, err = attest("statement", "--state", state, "--task-id", out["task_id"],
                          "--attempt-id", out["attempt_id"], "--candidate", cand,
                          "--out", bundle, "--sign")  # fmt: skip
    assert code == 0, err
    return bundle, pub


def fresh(tmp_path, name, source, *shas):
    repo = tmp_path / name
    git(tmp_path, "init", "-q", str(repo))
    for sha in shas:
        git(repo, "fetch", "-q", str(source), sha)
    return repo


def check(bundle, pub, repo, cand, *extra):
    code, stdout, _ = attest("verify", bundle, "--public-key", pub, "--repo", repo,
                             "--candidate", cand, "--json", *extra)  # fmt: skip
    return code, json.loads(stdout)


@sandbox
def test_an_external_candidate_receipt_verifies_offline_after_the_scratch_is_gone(
    project, state, tmp_path
):
    base, test_commit = make_test_commit(project, {"tests/test_regression.py": REGRESSION})
    cand = candidate(project, base, {"src/calc.py": FIX})
    code, out = verify(state, project, base, cand, "--test-commit", test_commit, tests=(VERIFIER,))
    assert code == 0, out
    assert out["core_status"] == "VERIFIED"
    binding = out["binding"]
    assert binding["head"] == cand and binding["base"] == base
    assert binding["test_commit"] == test_commit and binding["rule"] == "overlay-v1"
    assert out["executed_tree"] == binding["executed_tree"] and out["judged_commit"] != cand
    shutil.rmtree(state / "verify-scratch")
    shutil.rmtree(state / "verify-runs", ignore_errors=True)
    bundle, pub = signed(state, out, tmp_path, cand)

    origin = project.parent / "origin.git"  # where the deferred tests were published
    full = fresh(tmp_path, "full", project, cand)
    git(full, "fetch", "-q", str(origin), test_commit)
    code, result = check(bundle, pub, full, cand)
    assert code == 0 and result["ok"], result
    assert result["assurance"]["mode"] == "full" and result["test_source"] == "auto"

    conf = fresh(tmp_path, "conf", project, cand)
    code, result = check(bundle, pub, conf, cand)
    assert code == 1 and result["code"] == "POLICY_REQUIRED"
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({
        "rule": "overlay-v1", "test_commit": test_commit,
        "core_identity": default_core_identity(),
        "key_id": keyid(load_private_key(state).public_key()),
    }))  # fmt: skip
    code, result = check(bundle, pub, conf, cand, "--policy", policy)
    assert code == 0 and result["ok"], result
    assert result["assurance"]["test_contents"] == "not_verified"
    assert check(bundle, pub, full, base, "--policy", policy)[1]["code"] == "HEAD_MISMATCH"


@sandbox
def test_without_a_test_commit_the_receipt_stays_bound_to_the_commit(project, state, tmp_path):
    base = base_of(project)
    cand = candidate(project, base, {"src/calc.py": FIX})
    code, out = verify(state, project, base, cand)
    assert code == 0, out
    assert out["core_status"] == "VERIFIED"
    assert out["binding"] is None and out["executed_tree"] == "" and out["judged_commit"] == cand
    bundle, pub = signed(state, out, tmp_path, cand)
    code, result = check(bundle, pub, fresh(tmp_path, "r", project, cand), cand)
    assert code == 0 and result["ok"] and result["assurance"] is None, result
