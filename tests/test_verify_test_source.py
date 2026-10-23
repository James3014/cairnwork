"""``cairnwork-verify`` journals the hidden-test mode like the worker path does (Issue #175),
so ``cairnwork-attest`` can state it for a deferred-verification run too."""

import json

import pytest
from test_deferred_store import make_bundle, used_dir
from test_deferred_tests import CHECK, REGRESSION, make_test_commit
from verify_helpers import FIX, base_of, candidate, verify

from cairnwork.deferred_store import DeferredStore
from cairnwork_attest.statement import run_test_source

sandbox = pytest.mark.sandbox
VERIFIER = " ".join(CHECK[0][1])


def _config(state, **extra):
    (state / "config.json").write_text(json.dumps({"verifier_account": False, **extra}))


@sandbox
def test_a_bundle_mode_verify_run_is_attested_as_bundle_whatever_the_config_becomes(
    project, state, tmp_path
):
    base, sha, bundle = make_bundle(project, {"tests/test_regression.py": REGRESSION}, tmp_path)
    _config(state, test_source="bundle")
    store = DeferredStore.for_state(state, "bundle")
    store.path.mkdir(parents=True, mode=0o700, exist_ok=True)
    store.import_bundle(project, bundle, used_dir(store))
    cand = candidate(project, base, {"src/calc.py": FIX})
    code, out = verify(state, project, base, cand, "--test-commit", sha, tests=(VERIFIER,))
    assert code == 0, out
    assert run_test_source(state, out["task_id"], out["attempt_id"]) == "bundle"
    _config(state, test_source="auto")  # flipped afterwards
    assert run_test_source(state, out["task_id"], out["attempt_id"]) == "bundle"


@sandbox
def test_an_auto_mode_verify_run_is_attested_as_auto(project, state):
    base, test_commit = make_test_commit(project, {"tests/test_regression.py": REGRESSION})
    cand = candidate(project, base, {"src/calc.py": FIX})
    code, out = verify(state, project, base, cand, "--test-commit", test_commit, tests=(VERIFIER,))
    assert code == 0, out
    _config(state, test_source="bundle")
    assert run_test_source(state, out["task_id"], out["attempt_id"]) == "auto"


@sandbox
def test_a_verify_run_without_a_test_commit_is_attested_as_none(project, state):
    base = base_of(project)
    cand = candidate(project, base, {"src/calc.py": FIX})
    code, out = verify(state, project, base, cand)
    assert code == 0, out
    assert run_test_source(state, out["task_id"], out["attempt_id"]) == "none"
