"""Deferred tests: the worker never sees them, they must fail on the base, and the
commit that is verified and notarised carries the fix and the tests together."""

from pathlib import Path

import pytest
from conftest import commit_change, git
from test_pipeline import EditWorker

import cairnwork.deferred_tests as dt
from cairnwork.journal import Journal
from cairnwork.notary import Notary
from cairnwork.ownership import OwnershipLedger
from cairnwork.pipeline import TaskSpec, run_task

pytestmark = pytest.mark.sandbox


@pytest.fixture
def project(repo):
    commit_change(
        repo,
        {
            "src/calc.py": "def add(a, b):\n    return a + b + 1\n",
            "tests/conftest.py": (
                "import sys, pathlib\n"
                "sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / 'src'))\n"
            ),
        },
    )
    return repo


REGRESSION = "from calc import add\ndef test_regression():\n    assert add(1, 1) == 2\n"
CHECK = (("pytest", ("python3", "-m", "pytest", "-q", "tests/test_regression.py")),)


def make_test_commit(project, files):
    """A test commit on its own branch, whose parent is the current main."""
    base = git(project, "rev-parse", "HEAD")
    git(project, "checkout", "-q", "-b", "tests-only")
    test_commit = commit_change(project, files, message="tests")
    git(project, "checkout", "-q", "main")
    return base, test_commit


def run(project, tmp_path, base, test_commit, worker, verifiers=CHECK, key="k1"):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    task = TaskSpec(key, "card-" + key, "fix add", "add() is off by one", project, base,
                    ("src/calc.py",), verifiers, test_commit=test_commit)  # fmt: skip
    return run_task(
        task,
        ledger=OwnershipLedger(state / "own.sqlite"),
        notary=Notary(state / "notary.sqlite"),
        journal=Journal(state / "journal.sqlite"),
        worker=worker,
        run_root=state / "runs",
    )


class SeeingWorker(EditWorker):
    """Writes the fix and records whether the deferred test was visible."""

    def run(self, workdir: Path, task) -> None:
        self.saw_test = (workdir / "tests/test_regression.py").exists()
        super().run(workdir, task)


def test_fix_and_tests_are_verified_together_and_the_worker_never_sees_them(project, tmp_path):
    base, test_commit = make_test_commit(project, {"tests/test_regression.py": REGRESSION})
    worker = SeeingWorker({"src/calc.py": "def add(a, b):\n    return a + b\n"})
    result = run(project, tmp_path, base, test_commit, worker)
    assert result.status == "COMPLETED", result.reason
    assert worker.saw_test is False
    files = git(project, "diff", "--name-only", base, result.candidate_commit).split()
    assert sorted(files) == ["src/calc.py", "tests/test_regression.py"]
    assert git(project, "rev-parse", f"{result.candidate_commit}^^") == base


def test_tests_that_already_pass_on_the_base_are_refused(project, tmp_path):
    passing = "from calc import add\ndef test_regression():\n    assert add(1, 1) == 3\n"
    base, test_commit = make_test_commit(project, {"tests/test_regression.py": passing})
    worker = SeeingWorker({"src/calc.py": "x = 1\n"})
    result = run(project, tmp_path, base, test_commit, worker)
    assert result.status == "FAILED" and "TESTS_DO_NOT_FAIL_ON_BASE" in result.reason
    assert not hasattr(worker, "saw_test")  # the worker never started


def test_tests_that_only_break_on_the_base_are_refused(project, tmp_path):
    broken = "import no_such_module\ndef test_regression():\n    pass\n"
    base, test_commit = make_test_commit(project, {"tests/test_regression.py": broken})
    result = run(project, tmp_path, base, test_commit, SeeingWorker({}))
    assert "TESTS_DO_NOT_FAIL_ON_BASE" in result.reason


@pytest.mark.parametrize(
    ("files", "code"),
    [
        ({"src/calc.py": "x = 1\n"}, "TEST_COMMIT_SCOPE"),
        ({"tests/conftest.py": "x = 1\n"}, "TEST_COMMIT_SCOPE"),
    ],
)
def test_a_test_commit_may_only_add_plain_test_files(project, tmp_path, files, code):
    base, test_commit = make_test_commit(project, files)
    result = run(project, tmp_path, base, test_commit, SeeingWorker({}))
    assert code in result.reason


def test_a_test_commit_must_sit_on_the_base(project, tmp_path):
    base, test_commit = make_test_commit(project, {"tests/test_regression.py": REGRESSION})
    newer = commit_change(project, {"README.md": "moved\n"})
    result = run(project, tmp_path, newer, test_commit, SeeingWorker({}))
    assert "TEST_COMMIT_NOT_ON_BASE" in result.reason


class _Run:
    def __init__(self, code):
        self.returncode = code


@pytest.mark.parametrize(
    ("codes", "teeth"),
    [([1], True), ([0, 1], True), ([0], False), ([2], False), ([1, 2], False), ([None], False)],
)
def test_failed_on_base(codes, teeth):
    assert dt.failed_on_base([_Run(c) for c in codes]) is teeth
