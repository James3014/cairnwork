"""``test_roots``: where verifier targets and deferred tests may live (default: tests/)."""

import json
from pathlib import Path

import pytest
from conftest import commit_change, git
from test_deferred_tests import make_test_commit
from verify_helpers import FIX, base_of, candidate, verify

from cairnwork.deferred_tests import TestCommitRejected as Rejected
from cairnwork.deferred_tests import check_roots_on_base, ensure_test_commit
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
    ["src"], ["pyfiglet"], ["pkg/tests2"], ["Tests"], ["pkg/TESTS"], ["pkg/tests/sub"], ["test/x"],
    ["-tests"], ["-x/tests"], ["pkg/-x/tests"],
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
    (intake.state / "config.json").write_text(json.dumps({"test_roots": ["pkg/tests", "pkg/test"]}))
    assert intake.config()["test_roots"] == ["pkg/tests", "pkg/test"]


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


@pytest.mark.parametrize("name", ["test", "tests"])
def test_a_test_root_ends_in_test_or_tests_only(name):
    assert check_test_roots([f"a/{name}"]) == (f"a/{name}",)


@pytest.mark.parametrize(
    "name",
    ["conftest.py", "sitecustomize.py", "usercustomize.py", "setup.py", "x.pth", "X.PTH",
     "setup.cfg", "tox.ini", "pytest.ini", "pyproject.toml"],
)  # fmt: skip
def test_a_test_commit_refuses_the_names_a_candidate_may_not_touch(pkg_project, name):
    base, commit = make_test_commit(pkg_project, {f"pkg/tests/{name}": "x = 1\n"})
    with pytest.raises(Rejected, match="TEST_COMMIT_SCOPE"):
        paths_of(pkg_project, base, commit, PKG)


def test_an_init_file_is_allowed_in_a_test_commit(pkg_project):
    files = {"pkg/tests/__init__.py": "", "pkg/tests/test_regression.py": PKG_TEST}
    base, commit = make_test_commit(pkg_project, files)
    assert len(paths_of(pkg_project, base, commit, PKG)) == 2


def _base_with(repo, files, links=None):
    for path, target in (links or {}).items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).symlink_to(target)
    commit_change(repo, files)
    return git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize("links", [{"pkg/tests": "../src"}, {"pkg": "src"}])
def test_a_symlinked_root_or_parent_is_refused_on_the_base(pkg_project, links):
    git(pkg_project, "rm", "-rq", "pkg")
    base = _base_with(pkg_project, {"src/keep.py": "x = 1\n"}, links)
    with pytest.raises(Rejected, match="TEST_ROOT_NOT_A_DIRECTORY"):
        check_roots_on_base(pkg_project, base, PKG)


def test_a_root_that_is_a_file_is_refused_and_a_missing_or_real_one_passes(pkg_project):
    base = _base_with(pkg_project, {"flat/tests": "not a directory\n"})
    with pytest.raises(Rejected, match="TEST_ROOT_NOT_A_DIRECTORY"):
        check_roots_on_base(pkg_project, base, ("flat/tests",))
    check_roots_on_base(pkg_project, base, ("pkg/tests", "absent/tests", "src/test"))


def test_verify_refuses_a_symlinked_root_before_running_anything(pkg_project, pkg_state):
    git(pkg_project, "rm", "-rq", "pkg")
    base = _base_with(pkg_project, {"src/keep.py": "x = 1\n"}, {"pkg/tests": "../src"})
    cand = candidate(pkg_project, base, {"src/calc.py": "x = 1\n"})
    code, out = verify(
        pkg_state, pkg_project, base, cand, tests=("python3 -m pytest -q pkg/tests/a.py",)
    )
    assert code == 2 and "TEST_ROOT_NOT_A_DIRECTORY" in out["reason"]


def test_the_worker_path_refuses_a_symlinked_root_before_the_worker_starts(pkg_project, tmp_path):
    from test_pipeline import EditWorker

    git(pkg_project, "rm", "-rq", "pkg")
    base = _base_with(pkg_project, {"src/keep.py": "x = 1\n"}, {"pkg/tests": "../src"})
    verifier = (("v", (*RUN, "pkg/tests/test_x.py")),)
    task = TaskSpec("k", "c", "t", "i", pkg_project, base, ("src/calc.py",), verifier,
                    test_roots=PKG)  # fmt: skip
    state = tmp_path / "wstate"
    state.mkdir()
    result = _run(task, state, EditWorker({"src/calc.py": "x = 2\n"}))
    assert "TEST_ROOT_NOT_A_DIRECTORY" in result.reason and not result.verified


def _run(task, state, worker):
    from cairnwork.journal import Journal
    from cairnwork.notary import Notary
    from cairnwork.ownership import OwnershipLedger
    from cairnwork.pipeline import run_task

    return run_task(
        task, ledger=OwnershipLedger(state / "own.sqlite"), notary=Notary(state / "n.sqlite"),
        journal=Journal(state / "j.sqlite"), worker=worker, run_root=state / "runs",
    )  # fmt: skip


def test_a_case_variant_symlink_of_a_root_is_refused(pkg_project):
    base = _base_with(pkg_project, {"src/keep.py": "x = 1\n"}, {"Tests": "src"})
    with pytest.raises(Rejected, match="TEST_ROOT_NOT_A_DIRECTORY"):
        check_roots_on_base(pkg_project, base, ("tests",))
    base = _base_with(pkg_project, {"a/Pkg/x.py": "x = 1\n"}, {"a/PKG": "../src"})
    with pytest.raises(Rejected, match="TEST_ROOT_NOT_A_DIRECTORY"):
        check_roots_on_base(pkg_project, base, ("a/pkg/tests",))


def test_two_case_variants_of_a_root_are_refused(pkg_project, tmp_path):
    blob_file = (
        tmp_path / "blob.txt"
    )  # built in the index: a case-insensitive disk cannot hold both
    blob_file.write_text("x = 1\n")
    blob = git(pkg_project, "hash-object", "-w", str(blob_file))
    for name in ("Tests/a.py", "tests/b.py"):
        git(pkg_project, "update-index", "--add", "--cacheinfo", f"100644,{blob},{name}")
    git(pkg_project, "commit", "-q", "-m", "two variants")
    base = git(pkg_project, "rev-parse", "HEAD")
    with pytest.raises(Rejected, match="TEST_ROOT_NOT_A_DIRECTORY"):
        check_roots_on_base(pkg_project, base, ("tests",))


def test_tests_with_teeth_checks_the_roots_before_any_base_run(pkg_project, tmp_path, monkeypatch):
    import cairnwork.pipeline as pipeline

    git(pkg_project, "rm", "-rq", "pkg")
    base = _base_with(pkg_project, {"src/keep.py": "x = 1\n"}, {"pkg/tests": "../src"})
    task = TaskSpec("k", "c", "t", "i", pkg_project, base, ("src/calc.py",), (), test_roots=PKG,
                    test_commit="a" * 40)  # fmt: skip

    def boom(*args, **kwargs):
        raise AssertionError("the base was run")

    monkeypatch.setattr(pipeline, "run_verifiers", boom)
    monkeypatch.setattr(pipeline, "ensure_test_commit", boom)
    with pytest.raises(Rejected, match="TEST_ROOT_NOT_A_DIRECTORY"):
        pipeline.tests_with_teeth(task, (), tmp_path, None, (None, None))


@pytest.mark.parametrize(
    "path",
    ["pkg/tests/.pth", "pkg/tests/a.PTH", "pkg/tests/sitecustomize/__init__.py",
     "pkg/tests/UserCustomize/test_x.py", "pkg/tests/sub/sitecustomize.py"],
)  # fmt: skip
def test_a_test_commit_refuses_pth_files_and_site_packages(pkg_project, path):
    base, commit = make_test_commit(pkg_project, {path: "x = 1\n"})
    with pytest.raises(Rejected, match="TEST_COMMIT_SCOPE"):
        paths_of(pkg_project, base, commit, PKG)


def test_a_root_directory_itself_is_protected():
    two = ("tests", "pkg/tests")
    assert protected_verifier_inputs(("pkg/tests", "tests", "pkg"), two) == ("pkg/tests", "tests")
    with pytest.raises(TaskRejected, match="PROTECTED_VERIFIER_INPUT"):
        check_task(_task(("pkg/tests",), (*RUN, "pkg/tests/test_x.py"), PKG))
