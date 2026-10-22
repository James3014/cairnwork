from pathlib import Path

import pytest
from conftest import commit_change, git

from cairnwork.issue_task import PROTECTED_NAMES, PROTECTED_PATHS, is_protected
from cairnwork.journal import Journal
from cairnwork.notary import Notary
from cairnwork.ownership import OwnershipLedger
from cairnwork.pipeline import TaskRejected, TaskSpec, run_task
from cairnwork.verifiers import protected_verifier_inputs

VERIFY = (("pytest", ("python3", "-m", "pytest", "-q", "tests/test_calc.py")),)

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
    return (
        OwnershipLedger(tmp_path / "own.sqlite"),
        Notary(tmp_path / "notary.sqlite"),
        Journal(tmp_path / "journal.sqlite"),
    )


def go(project, stores, tmp_path, worker, **kwargs):
    ledger, notary, journal = stores
    return run_task(
        task(project, **kwargs),
        ledger=ledger,
        notary=notary,
        journal=journal,
        worker=worker,
        run_root=tmp_path / "runs",
    )


def test_correct_fix_completes_with_core_verified(project, stores, tmp_path):
    result = go(
        project, stores, tmp_path, EditWorker({"src/calc.py": "def add(a, b):\n    return a + b\n"})
    )
    assert result.status == "COMPLETED" and result.verified, result.reason
    assert stores[0].get(task_id=result.task_id).status == "COMPLETED"


def test_existing_root_module_completes_with_core_verified(repo, stores, tmp_path):
    commit_change(
        repo,
        {
            "calc.py": "def add(a, b):\n    return a + b + 1\n",
            "tests/test_calc.py": (
                "from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n"
            ),
        },
    )
    result = go(
        repo,
        stores,
        tmp_path,
        EditWorker({"calc.py": "def add(a, b):\n    return a + b\n"}),
        allowed=("calc.py",),
    )
    assert result.status == "COMPLETED" and result.verified, result.reason


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
        ({"allowed": ("src/.GIT/config",)}, "BAD_SCOPE"),
        ({"allowed": ("src/calc.py\n",)}, "BAD_SCOPE"),
        ({"allowed": (".github/workflows/x.yml",)}, "TASK_ALLOWED_FILES_PROTECTED"),
        ({"allowed": ("AGENTS.md",)}, "TASK_ALLOWED_FILES_PROTECTED"),
        ({"allowed": ("agents.MD",)}, "TASK_ALLOWED_FILES_PROTECTED"),
        ({"allowed": ("uv.lock",)}, "TASK_ALLOWED_FILES_PROTECTED"),
        ({"allowed": ("src/calc.py", "docs/governance/x.md")}, "TASK_ALLOWED_FILES_PROTECTED"),
        ({"allowed": ("Pyproject.TOML",)}, "TASK_ALLOWED_FILES_PROTECTED"),
        ({"verifiers": (("v", ("bash", "-c", "true")),)}, "VERIFIER_NOT_ALLOWED"),
    ],
)
def test_bad_tasks_are_rejected_before_anything_runs(project, stores, tmp_path, kwargs, code):
    with pytest.raises(TaskRejected) as info:
        go(project, stores, tmp_path, CrashWorker(), **kwargs)
    assert info.value.code == code
    assert not (tmp_path / "runs").exists()


def test_submit_path_refuses_everything_the_issue_path_protects(project, stores, tmp_path):
    sampled: set[str] = set()
    rules = [r.replace("*", "x") for r in (*PROTECTED_PATHS, *PROTECTED_NAMES)]
    for path in (
        *rules,
        *(f"d/{n}" for n in PROTECTED_NAMES),
        *(f"{r}/y.py" for r in PROTECTED_PATHS if "*" not in r),
    ):
        assert is_protected(path)
        with pytest.raises(TaskRejected) as info:
            go(project, stores, tmp_path, CrashWorker(), allowed=(path,))
        earlier = "PROTECTED_VERIFIER_INPUT" if protected_verifier_inputs((path,)) else None
        assert info.value.code == (earlier or "TASK_ALLOWED_FILES_PROTECTED"), path
        sampled.add(info.value.code)
    assert sampled == {"TASK_ALLOWED_FILES_PROTECTED", "PROTECTED_VERIFIER_INPUT"}


class ChmodWorker:
    def run(self, workdir, _task):
        (workdir / "src").chmod(0)


def test_infrastructure_errors_end_failed_with_a_reason(project, stores, tmp_path):
    try:
        result = go(project, stores, tmp_path, ChmodWorker())
    finally:
        for path in (tmp_path / "runs").rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o755)
    assert result.status == "FAILED" and not result.verified
    assert result.reason
    assert stores[0].get(task_id=result.task_id).status == "FAILED"


def test_bad_base_commit_is_rejected_before_binding(project, stores, tmp_path):
    ledger, notary, journal = stores
    bad = TaskSpec(**{**task(project).__dict__, "base_commit": "HEAD"})
    with pytest.raises(TaskRejected) as info:
        run_task(
            bad,
            ledger=ledger,
            notary=notary,
            journal=journal,
            worker=CrashWorker(),
            run_root=tmp_path / "r",
        )
    assert info.value.code == "BAD_BASE"


def test_unbound_core_response_reports_its_reason_not_core_verified(project, tmp_path):
    from product.protocol import core_identity

    pinned = {**core_identity(), "version": "pinned-elsewhere"}
    stores = (
        OwnershipLedger(tmp_path / "own.sqlite"),
        Notary(tmp_path / "notary.sqlite", core_identity=pinned),
        Journal(tmp_path / "journal.sqlite"),
    )
    fix = EditWorker({"src/calc.py": "def add(a, b):\n    return a + b\n"})
    result = go(project, stores, tmp_path, fix)
    assert result.status == "FAILED" and not result.verified
    assert result.reason == "CORE_IDENTITY_MISMATCH"
