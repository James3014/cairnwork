"""Deferred tests: the worker never sees them, they must fail on the base, and the
commit that is verified and notarised carries the fix and the tests together."""

import subprocess
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
FIX = {"src/calc.py": "def add(a, b):\n    return a + b\n"}
CHECK = (("pytest", ("python3", "-m", "pytest", "-q", "tests/test_regression.py")),)


def make_test_commit(project, files):
    """A test commit on the base that exists only in the origin, as on GitHub: the
    project (Cairnwork's repository) gets it only when Cairnwork fetches it."""
    base = git(project, "rev-parse", "HEAD")
    root = Path(project).parent
    origin = root / "origin.git"
    if not origin.exists():
        git(root, "clone", "-q", "--bare", str(project), str(origin))
        git(project, "remote", "add", "origin", str(origin))
    work = root / f"author-{len(list(root.glob('author-*')))}"
    git(root, "clone", "-q", str(origin), str(work))
    git(work, "config", "user.email", "t@e")
    git(work, "config", "user.name", "t")
    git(work, "checkout", "-q", "--detach", base)
    test_commit = commit_change(work, files, message="tests")
    git(work, "push", "-q", "origin", f"{test_commit}:refs/heads/tests-{test_commit[:8]}")
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
    result = run(project, tmp_path, base, test_commit, SeeingWorker(FIX))
    assert result.status == "FAILED" and "TESTS_DO_NOT_FAIL_ON_BASE" in result.reason


def test_tests_that_only_break_on_the_base_are_refused(project, tmp_path):
    broken = "import no_such_module\ndef test_regression():\n    pass\n"
    base, test_commit = make_test_commit(project, {"tests/test_regression.py": broken})
    result = run(project, tmp_path, base, test_commit, SeeingWorker(FIX))
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
    result = run(project, tmp_path, base, test_commit, SeeingWorker(FIX))
    assert code in result.reason


def test_a_test_commit_must_sit_on_the_base(project, tmp_path):
    base, test_commit = make_test_commit(project, {"tests/test_regression.py": REGRESSION})
    newer = commit_change(project, {"README.md": "moved\n"})
    result = run(project, tmp_path, newer, test_commit, SeeingWorker(FIX))
    assert "TEST_COMMIT_NOT_ON_BASE" in result.reason


class _Run:
    """A stand-in VerifierRun: ``failing`` names the test modules whose case failed; the
    trusted report_guard outcomes are keyed by node id (``tests/<file>.py::t``)."""

    _MODULE_FILE = {
        "tests.test_regression": "tests/test_regression.py",
        "tests.test_other": "tests/test_other.py",
    }

    def __init__(self, tmp_path, name, code, failing=()):
        self.log_path = tmp_path / name / "verifier.log"
        self.returncode = code
        self.outcomes = {
            f"{path}::t": ("failed" if module in failing else "passed")
            for module, path in self._MODULE_FILE.items()
        }


PATHS = ("tests/test_regression.py",)


def test_teeth_need_a_failing_case_from_the_deferred_files(tmp_path):
    assert dt.failed_on_base([_Run(tmp_path, "a", 1, {"tests.test_regression"})], PATHS)
    # another file failing does not count, even with exit 1
    assert not dt.failed_on_base([_Run(tmp_path, "b", 1, {"tests.test_other"})], PATHS)
    # passing everywhere, or a verifier breaking otherwise, does not count
    assert not dt.failed_on_base([_Run(tmp_path, "c", 0)], PATHS)
    assert not dt.failed_on_base(
        [_Run(tmp_path, "d", 1, {"tests.test_regression"}), _Run(tmp_path, "e", 2)], PATHS
    )
    assert not dt.failed_on_base([_Run(tmp_path, "f", None)], PATHS)


class ScanningWorker(EditWorker):
    """Writes the fix and records anything readable that holds the test."""

    def __init__(self, files, test_commit):
        super().__init__(files)
        self.test_commit = test_commit

    def run(self, workdir: Path, task) -> None:
        probe = subprocess.run(
            ["git", "-C", str(task.repo), "cat-file", "-e", f"{self.test_commit}^{{commit}}"],
            capture_output=True, check=False,
        )  # fmt: skip
        self.has_object = probe.returncode == 0
        self.found = [str(p) for p in Path(task.repo).rglob("*") if _holds(p)] + [
            str(p) for p in workdir.parent.rglob("*") if _holds(p)
        ]
        super().run(workdir, task)


def _holds(path: Path) -> bool:
    try:
        return path.is_file() and b"add(1, 1) == 2" in path.read_bytes()
    except OSError:
        return False


def test_nothing_the_worker_can_read_holds_the_deferred_test(project, tmp_path):
    base, test_commit = make_test_commit(project, {"tests/test_regression.py": REGRESSION})
    worker = ScanningWorker(FIX, test_commit)
    result = run(project, tmp_path, base, test_commit, worker)
    assert result.status == "COMPLETED", result.reason
    assert worker.found == [] and worker.has_object is False  # fetched after the worker
    assert list((tmp_path / "state" / "runs").glob("*-b-v*")) == []  # base-run logs dropped


def test_a_retry_with_the_same_test_commit_is_refused(project, tmp_path):
    base, test_commit = make_test_commit(project, {"tests/test_regression.py": REGRESSION})
    first = run(project, tmp_path, base, test_commit, SeeingWorker({"src/calc.py": "x = 1\n"}))
    assert first.status == "FAILED"
    worker = SeeingWorker(FIX)
    second = run(project, tmp_path, base, test_commit, worker, key="k2")
    assert "TEST_COMMIT_ALREADY_EXPOSED" in second.reason
    assert not hasattr(worker, "saw_test")  # refused before any worker started


# an oversized or forged verdict file is rejected by report_guard.read_verdict
# (see tests/test_report_guard.py); failed_on_base works on the trusted outcomes.
