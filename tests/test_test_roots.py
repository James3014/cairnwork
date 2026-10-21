"""``test_roots``: where verifier targets and deferred tests may live (default: tests/)."""

import json
from pathlib import Path

import pytest
from conftest import commit_change
from test_deferred_tests import make_test_commit
from verify_helpers import FIX, base_of, candidate, verify

from cairnwork.deferred_tests import TestCommitRejected as Rejected
from cairnwork.deferred_tests import ensure_test_commit
from cairnwork.deferred_tests import test_paths as _paths
from cairnwork.intake import Intake, IntakeRefused
from cairnwork.issue_task import IssueRefused, check_test_roots, parse_task_block
from cairnwork.pipeline import TaskRejected, TaskSpec, check_task
from cairnwork.verifiers import VerifierRejected, parse_verifier, protected_verifier_inputs

PKG = ("pkg/tests",)
RUN = ["python3", "-m", "pytest", "-q"]
sandbox = pytest.mark.sandbox


def test_default_roots_behave_as_before():
    assert parse_verifier("v", [*RUN, "tests/test_x.py"]).targets == ("tests/test_x.py",)
    with pytest.raises(VerifierRejected):
        parse_verifier("v", [*RUN, "pkg/tests/test_x.py"])
    assert protected_verifier_inputs(("tests/a.py", "src/a.py")) == ("tests/a.py",)


def test_verifier_targets_follow_the_roots():
    assert parse_verifier("v", [*RUN, "pkg/tests/t/test_x.py::test_a"], PKG).targets
    for bad in ("tests/test_x.py", "pkg/tests_x/test_x.py", "pkg/test_x.py", "pkg/tests/../a.py"):
        with pytest.raises(VerifierRejected):
            parse_verifier("v", [*RUN, bad], PKG)
    two = ("tests", "pkg/tests")
    assert parse_verifier("v", [*RUN, "tests/a.py", "pkg/tests/b.py"], two).targets


def test_files_under_every_root_stay_protected():
    paths = ("pkg/tests/test_x.py", "pkg/tests/conftest.py", "pkg/src/a.py", "tests/a.py")
    assert protected_verifier_inputs(paths, PKG) == paths[:2]
    assert protected_verifier_inputs(("pkg/conftest.py",), PKG) == ("pkg/conftest.py",)


def _task(allowed, verifier, roots):
    return TaskSpec("k", "c", "t", "i", Path("/r"), "a" * 40, allowed, (("v", verifier),),
                    test_roots=roots)  # fmt: skip


def test_check_task_applies_the_roots():
    check_task(_task(("pkg/a.py",), (*RUN, "pkg/tests/test_x.py"), PKG))
    with pytest.raises(TaskRejected, match="VERIFIER_NOT_ALLOWED"):
        check_task(_task(("pkg/a.py",), (*RUN, "tests/test_x.py"), PKG))
    for path in ("pkg/tests/test_new.py", "pkg/tests/conftest.py"):
        with pytest.raises(TaskRejected, match="PROTECTED_VERIFIER_INPUT"):
            check_task(_task((path,), (*RUN, "pkg/tests/test_x.py"), PKG))


def test_issue_blocks_follow_the_roots():
    def block(command):
        raw = {"difficulty": "easy", "allowed_files": ["pkg/a.py"], "verification": [command],
               "capability_ids": ["x"]}  # fmt: skip
        return f"```eia-task\n{json.dumps(raw)}\n```"

    task = parse_task_block(block("python3 -m pytest -q pkg/tests/test_x.py"), PKG)
    assert task.verifiers == (("python3", "-m", "pytest", "-q", "pkg/tests/test_x.py"),)
    with pytest.raises(IssueRefused, match="TASK_VERIFICATION_UNSUPPORTED"):
        parse_task_block(block("python3 -m pytest -q tests/test_x.py"), PKG)
    with pytest.raises(IssueRefused, match="TASK_VERIFICATION_UNSUPPORTED"):
        parse_task_block(block("python3 -m pytest -q pkg/tests/test_x.py"))


BAD_ROOTS = [
    "pkg/tests", [], "tests", [""], ["."], ["/tests"], ["/abs/tests"], ["../tests"],
    ["pkg/../tests"], ["pkg/./tests"], ["tests/"], ["pkg//tests"], ["tests", "tests"],
    ["pkg/*"], ["pkg/te?ts"], ["pkg/[ab]"], ["pkg/tests\n"], ["pkg tests"], [".hidden/tests"],
    [".github"], [".github/tests"], ["docs/governance"], ["tasks"], [5], [None], [["tests"]],
    ["t1", "t2", "t3", "t4", "t5", "t6", "t7", "t8", "t9"],
]  # fmt: skip


@pytest.mark.parametrize("roots", BAD_ROOTS)
def test_bad_roots_are_refused(tmp_path, roots):
    if roots in ("tests",):
        roots = "tests"  # a bare string is not a list
    with pytest.raises(ValueError):
        check_test_roots(roots)
    intake = Intake(tmp_path / "state")
    intake.prepare()
    (intake.state / "config.json").write_text(json.dumps({"test_roots": roots}))
    with pytest.raises(IntakeRefused, match="BAD_CONFIG"):
        intake.config()


def test_good_roots_load_and_the_default_is_tests(tmp_path):
    intake = Intake(tmp_path / "state")
    intake.prepare()
    assert intake.config()["test_roots"] == ["tests"]
    (intake.state / "config.json").write_text(json.dumps({"test_roots": ["pkg/tests", "t_2"]}))
    assert intake.config()["test_roots"] == ["pkg/tests", "t_2"]


@pytest.fixture
def pkg_project(repo):
    commit_change(
        repo,
        {
            "src/calc.py": "def add(a, b):\n    return a + b + 1\n",
            "pkg/tests/conftest.py": (
                "import sys, pathlib\n"
                "sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / 'src'))\n"
            ),
        },
    )
    return repo


PKG_TEST = "from calc import add\ndef test_regression():\n    assert add(1, 1) == 2\n"


def paths_of(repo, base, commit, *roots):
    ensure_test_commit(repo, commit)
    return _paths(repo, base, commit, *roots)


def test_a_deferred_test_commit_must_sit_under_a_root(pkg_project, tmp_path):
    base, ok = make_test_commit(pkg_project, {"pkg/tests/test_regression.py": PKG_TEST})
    assert paths_of(pkg_project, base, ok, PKG) == ("pkg/tests/test_regression.py",)
    with pytest.raises(Rejected, match="TEST_COMMIT_SCOPE"):
        paths_of(pkg_project, base, ok)  # default roots: not under tests/
    for files in ({"pkg/tests/conftest.py": "x = 1\n"}, {"tests/test_r.py": PKG_TEST},
                  {"pkg/tests_x/test_r.py": PKG_TEST}):  # fmt: skip
        base, bad = make_test_commit(pkg_project, files)
        with pytest.raises(Rejected, match="TEST_COMMIT_SCOPE"):
            paths_of(pkg_project, base, bad, PKG)


@pytest.fixture
def pkg_state(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    config = {"verifier_account": False, "test_roots": ["pkg/tests"]}
    (state / "config.json").write_text(json.dumps(config))
    (state / "ACCEPTING").touch()
    return state


@sandbox
def test_a_deferred_test_under_a_root_verifies_end_to_end(pkg_project, pkg_state):
    base, test_commit = make_test_commit(pkg_project, {"pkg/tests/test_regression.py": PKG_TEST})
    cand = candidate(pkg_project, base, {"src/calc.py": FIX["src/calc.py"]} if False else {
        "src/calc.py": "def add(a, b):\n    return a + b\n"})  # fmt: skip
    command = "python3 -m pytest -q pkg/tests/test_regression.py"
    code, out = verify(pkg_state, pkg_project, base, cand, "--test-commit", test_commit,
                       tests=(command,))  # fmt: skip
    assert code == 0 and out["verified"] is True, out
    assert out["judged_commit"] != cand


def test_verify_refuses_targets_outside_the_roots(pkg_project, pkg_state):
    base = base_of(pkg_project)
    cand = candidate(pkg_project, base, {"src/calc.py": "x = 1\n"})
    code, out = verify(
        pkg_state, pkg_project, base, cand, tests=("python3 -m pytest -q tests/a.py",)
    )
    assert code == 2 and "VERIFIER_NOT_ALLOWED" in json.dumps(out)
