"""``cairnwork-verify``: a deferred test commit is single-use (it is exposed by the run)."""

import json

import pytest
from conftest import git
from test_deferred_tests import CHECK, REGRESSION, make_test_commit
from verify_helpers import FIX, candidate, make_mirror, verify

sandbox = pytest.mark.sandbox


@sandbox
def test_a_test_commit_is_burned_by_the_run_that_uses_it(project, state):
    base, test_commit = make_test_commit(project, {"tests/test_regression.py": REGRESSION})
    verifier = " ".join(CHECK[0][1])
    bad = candidate(project, base, {"src/calc.py": FIX, "src/other.py": "x = 1\n"})
    code, out = verify(state, project, base, bad, "--test-commit", test_commit, tests=(verifier,))
    assert out["reason"].startswith("OUT_OF_SCOPE")  # refused before the tests were fetched
    good = candidate(project, base, {"src/calc.py": FIX})
    code, out = verify(state, project, base, good, "--test-commit", test_commit, tests=(verifier,))
    assert code == 0, out
    assert (state / "verify-used-tests" / test_commit).exists()
    other = candidate(project, base, {"src/calc.py": FIX + "# again\n"})
    code, out = verify(state, project, base, other, "--test-commit", test_commit, tests=(verifier,))
    assert code == 2 and out["reason"].startswith("TEST_COMMIT_ALREADY_EXPOSED")


@sandbox
def test_the_burn_is_written_before_the_tests_are_fetched_and_survives_a_crash(
    project, state, monkeypatch
):
    import cairnwork_verify.verify as module
    from cairnwork.intake import Intake

    base, test_commit = make_test_commit(project, {"tests/test_regression.py": REGRESSION})
    verifier = tuple(" ".join(CHECK[0][1]).split())
    cand = candidate(project, base, {"src/calc.py": FIX})

    def crash(*_args):
        raise RuntimeError("power cut")

    monkeypatch.setattr(module, "tests_with_teeth", crash)
    with pytest.raises(RuntimeError):
        module.verify_candidate(
            Intake(state), repo=project, base=base, candidate=cand,
            allowed_files=("src/calc.py",), verifiers=(("v", verifier),), test_commit=test_commit,
        )  # fmt: skip
    monkeypatch.undo()
    code, out = verify(
        state, project, base, cand, "--test-commit", test_commit, tests=(" ".join(verifier),)
    )
    assert code == 2 and out["reason"].startswith("TEST_COMMIT_ALREADY_EXPOSED")


@sandbox
def test_a_relative_origin_still_reaches_the_deferred_tests(project, state):
    import os

    base, test_commit = make_test_commit(project, {"tests/test_regression.py": REGRESSION})
    absolute = git(project, "remote", "get-url", "origin")
    git(project, "remote", "set-url", "origin", os.path.relpath(absolute, project))
    cand = candidate(project, base, {"src/calc.py": FIX})
    verifier = " ".join(CHECK[0][1])
    code, out = verify(state, project, base, cand, "--test-commit", test_commit, tests=(verifier,))
    assert code == 0, out


@sandbox
def test_a_test_commit_is_single_use_through_a_mirror_too(project, state, tmp_path):
    base, test_commit = make_test_commit(project, {"tests/test_regression.py": REGRESSION})
    mirror = make_mirror(project, tmp_path)
    (state / "config.json").write_text(
        json.dumps({"verifier_account": False, "mirror": str(mirror)})
    )
    verifier = " ".join(CHECK[0][1])
    wrong = candidate(project, base, {"src/calc.py": "def add(a, b):\n    return a * b\n"})
    code, out = verify(state, project, base, wrong, "--test-commit", test_commit, tests=(verifier,))
    assert code == 2 and out["verified"] is False  # a wrong candidate still uses the tests up
    good = candidate(project, base, {"src/calc.py": FIX})
    code, out = verify(state, project, base, good, "--test-commit", test_commit, tests=(verifier,))
    assert code == 2 and out["reason"].startswith("TEST_COMMIT_ALREADY_EXPOSED")
