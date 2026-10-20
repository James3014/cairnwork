from pathlib import Path

import pytest
from conftest import commit_change, git

from cairnwork.notary import Notary
from cairnwork.ownership import OwnershipLedger
from cairnwork.pipeline import TaskRejected, TaskSpec, run_task

VERIFY = (("pytest", ("python3", "-m", "pytest", "-q", "tests/test_calc.py")),)


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
            "tests/test_calc.py": (
                "from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n"
            ),
        },
    )
    return repo


def task(project, key="k1", allowed=("src/calc.py",), verifiers=VERIFY):
    return TaskSpec(
        key,
        "card-" + key,
        "fix add",
        "add() is off by one",
        project,
        git(project, "rev-parse", "HEAD"),
        allowed,
        verifiers,
    )


class EditWorker:
    def __init__(self, files):
        self.files = files

    def run(self, workdir: Path, _task) -> None:
        for name, text in self.files.items():
            (workdir / name).parent.mkdir(parents=True, exist_ok=True)
            (workdir / name).write_text(text)


class CrashWorker:
    def run(self, workdir, _task):
        raise RuntimeError("model unavailable")


@pytest.fixture
def stores(tmp_path):
    return OwnershipLedger(tmp_path / "own.sqlite"), Notary(tmp_path / "notary.sqlite")


def go(project, stores, tmp_path, worker, **kwargs):
    ledger, notary = stores
    return run_task(
        task(project, **kwargs),
        ledger=ledger,
        notary=notary,
        worker=worker,
        run_root=tmp_path / "runs",
    )


def test_correct_fix_completes_with_core_verified(project, stores, tmp_path):
    result = go(
        project, stores, tmp_path, EditWorker({"src/calc.py": "def add(a, b):\n    return a + b\n"})
    )
    assert result.status == "COMPLETED" and result.verified, result.reason
    assert stores[0].get(task_id=result.task_id).status == "COMPLETED"


@pytest.mark.parametrize(
    "worker,reason",
    [
        (
            EditWorker({"src/calc.py": "def add(a, b):\n    return a - b\n"}),
            "CORE_FAILED_VERIFICATION",
        ),
        (
            EditWorker(
                {"src/calc.py": "def add(a, b):\n    return a + b\n", "src/other.py": "x = 1\n"}
            ),
            "DIRTY_TREE",
        ),
        (EditWorker({}), "NO_COMMIT"),
        (CrashWorker(), "WORKER_FAILED"),
    ],
    ids=["wrong-fix", "out-of-scope-edit", "no-change", "worker-crash"],
)
def test_failures_never_complete(project, stores, tmp_path, worker, reason):
    result = go(project, stores, tmp_path, worker)
    assert result.status == "FAILED" and not result.verified
    assert reason in result.reason
    assert stores[0].get(task_id=result.task_id).status == "FAILED"


def test_resend_does_not_run_again(project, stores, tmp_path):
    worker = EditWorker({"src/calc.py": "def add(a, b):\n    return a + b\n"})
    first = go(project, stores, tmp_path, worker)
    second = go(project, stores, tmp_path, CrashWorker())
    assert second.reason == "DUPLICATE_REQUEST" and second.status == first.status


@pytest.mark.parametrize(
    "kwargs,code",
    [
        ({"allowed": ("tests/test_calc.py",)}, "PROTECTED_VERIFIER_INPUT"),
        ({"allowed": ("src/calc.py", "pyproject.toml")}, "PROTECTED_VERIFIER_INPUT"),
        ({"allowed": ()}, "BAD_SCOPE"),
        ({"allowed": ("../x.py",)}, "BAD_SCOPE"),
        ({"allowed": ("/etc/passwd",)}, "BAD_SCOPE"),
        ({"allowed": (".git/config",)}, "BAD_SCOPE"),
        ({"allowed": ("src//calc.py",)}, "BAD_SCOPE"),
        ({"allowed": ("src/./calc.py",)}, "BAD_SCOPE"),
        ({"allowed": ("src/calc.py", "src/calc.py")}, "BAD_SCOPE"),
        ({"allowed": ("src/calc.py\n",)}, "BAD_SCOPE"),
        ({"verifiers": (("v", ("bash", "-c", "true")),)}, "VERIFIER_NOT_ALLOWED"),
    ],
)
def test_bad_tasks_are_rejected_before_anything_runs(project, stores, tmp_path, kwargs, code):
    with pytest.raises(TaskRejected) as info:
        go(project, stores, tmp_path, CrashWorker(), **kwargs)
    assert info.value.code == code
    assert not (tmp_path / "runs").exists()
