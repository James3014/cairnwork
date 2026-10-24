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
    import cairnwork.verify_candidate as module
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


@pytest.mark.parametrize("make", ["file", "dangling-link", "unreadable"])
def test_the_pre_check_never_reads_an_unknown_marker_as_unused(tmp_path, make):
    import os

    from cairnwork.deferred_tests import TestCommitRejected
    from cairnwork.intake import Intake
    from cairnwork.verify_candidate import _burn

    intake = Intake(tmp_path / "state")
    intake.prepare()
    oid = "b" * 40
    used = intake.state / "verify-used-tests"
    _burn(intake, oid, record=False)  # not used yet: passes
    marker = used / oid
    if make == "file":
        marker.write_text("")
    elif make == "dangling-link":
        marker.symlink_to(used / "gone")
    elif os.geteuid() == 0:
        pytest.skip("root can look inside a mode-000 directory")
    else:
        used.chmod(0o000)
    try:
        with pytest.raises(PermissionError if make == "unreadable" else TestCommitRejected):
            _burn(intake, oid, record=False)
    finally:
        used.chmod(0o700)


def test_a_marker_that_cannot_be_created_stops_the_run(tmp_path, monkeypatch):
    import os

    import cairnwork.verify_candidate as vc
    from cairnwork.intake import Intake

    intake = Intake(tmp_path / "state")
    intake.prepare()

    def vanished(path, *a, **kw):  # the directory went away between mkdir and open
        raise FileNotFoundError(2, os.strerror(2), str(path))

    monkeypatch.setattr(vc.os, "open", vanished)
    with pytest.raises(FileNotFoundError):
        vc._burn(intake, "c" * 40, record=True)


def test_the_record_is_exclusive(tmp_path):
    from cairnwork.deferred_tests import TestCommitRejected
    from cairnwork.intake import Intake
    from cairnwork.verify_candidate import _burn

    intake = Intake(tmp_path / "state")
    intake.prepare()
    _burn(intake, "d" * 40, record=True)
    with pytest.raises(TestCommitRejected, match="TEST_COMMIT_ALREADY_EXPOSED"):
        _burn(intake, "d" * 40, record=True)  # two runs racing past the pre-check: one wins


def test_a_used_commit_is_refused_before_any_scratch_clone(project, state, monkeypatch):
    import cairnwork.verify_candidate as module
    from cairnwork.intake import Intake

    base, test_commit = make_test_commit(project, {"tests/test_regression.py": REGRESSION})
    cand = candidate(project, base, {"src/calc.py": FIX})
    used = state / "verify-used-tests"
    used.mkdir(mode=0o700)
    (used / test_commit).write_text("")

    def no_scratch(*_args, **_kw):
        raise AssertionError("a used commit reached the scratch clone")

    monkeypatch.setattr(module, "_scratch", no_scratch)
    out = module.verify_candidate(
        Intake(state), repo=project, base=base, candidate=cand, allowed_files=("src/calc.py",),
        verifiers=(("v", tuple(" ".join(CHECK[0][1]).split())),), test_commit=test_commit,
    )  # fmt: skip
    assert out["reason"].startswith("TEST_COMMIT_ALREADY_EXPOSED")
