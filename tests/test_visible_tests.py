"""Visible-test feedback (experimental, Issue #32): the worker gets a second turn with the
pass/fail summary of the visible tests, and never reaches the deferred tests or the logs."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import commit_change, git
from test_deferred_tests import make_test_commit

from cairnwork.journal import Journal
from cairnwork.notary import Notary
from cairnwork.ownership import OwnershipLedger
from cairnwork.pipeline import TaskSpec, run_task
from experimental.visible_tests import VisibleTestsWorker, visible_specs
from experimental.visible_tests.feedback import HIDDEN, _set_aside_log, summarize

SECRET_LOG = "LOG-ONLY-7f3a91"  # appears in a failing test's assertion message, i.e. in the log
CALC_BUG = "def add(a, b):\n    return a + b + 1\n\n\ndef sub(a, b):\n    return a - b\n"
CALC_WRONG = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a + b\n"
CALC_FIX = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
VISIBLE = f"from calc import add, sub\ndef test_sub():\n    assert sub(3, 1) == 2, '{SECRET_LOG}'\n"
VISIBLE_ADD = f"from calc import add\ndef test_add():\n    assert add(1, 1) == 2, '{SECRET_LOG}'\n"
REGRESSION = "from calc import add\ndef test_regression():\n    assert add(2, 2) == 4\n"
HIDDEN_MARK = "test_regression"


def _verifier(vid, path):
    return (vid, ("python3", "-m", "pytest", "-q", path))


@pytest.fixture
def project(repo):
    commit_change(
        repo,
        {
            "src/calc.py": CALC_BUG,
            "tests/conftest.py": (
                "import sys, pathlib\n"
                "sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / 'src'))\n"
            ),
            "tests/test_sub.py": VISIBLE,
            "tests/test_add.py": VISIBLE_ADD,
        },
    )
    return repo


class TurnWorker:
    """Writes turns[i] on turn i and records what each turn could see."""

    def __init__(self, turns, test_commit=""):
        self.turns, self.test_commit = list(turns), test_commit
        self.instructions, self.saw = [], []

    def run(self, workdir: Path, task) -> None:
        self.instructions.append(task.instructions)
        self.saw.append(_visible_to_worker(workdir, task.repo, self.test_commit))
        text = self.turns[min(len(self.instructions), len(self.turns)) - 1]
        if text is not None:
            (workdir / "src/calc.py").write_text(text)


def _visible_to_worker(workdir: Path, repo: Path, test_commit: str) -> dict:
    """Everything a same-user worker could read next to its clone and in Cairnwork's repo."""
    near = [p for p in workdir.parent.rglob("*") if p.is_file() and not p.is_symlink()]
    holding = [str(p) for p in near if _contains(p, (SECRET_LOG, HIDDEN_MARK))]
    holding = [p for p in holding if not p.startswith(str(workdir) + "/tests/")]  # base tests
    logs = [str(p) for p in near if p.name == "verifier.log"]
    has = {}
    for where in (repo, workdir):
        if test_commit:
            probe = subprocess.run(
                ["git", "-C", str(where), "cat-file", "-e", f"{test_commit}^{{commit}}"],
                capture_output=True, check=False,
            )  # fmt: skip
            has[str(where)] = probe.returncode == 0
    return {"holding": holding, "logs": logs, "has_test_commit": has}


def _contains(path: Path, needles) -> bool:
    try:
        data = path.read_bytes()
    except OSError:
        return False
    return any(n.encode() in data for n in needles)


def _run(project, tmp_path, worker, verifiers, test_commit="", base=None):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    base = base or git(project, "rev-parse", "HEAD")
    task = TaskSpec("k1", "card-k1", "fix add", "add() is off by one", project, base,
                    ("src/calc.py",), verifiers, test_commit=test_commit)  # fmt: skip
    return run_task(
        task,
        ledger=OwnershipLedger(state / "own.sqlite"),
        notary=Notary(state / "notary.sqlite"),
        journal=Journal(state / "journal.sqlite"),
        worker=VisibleTestsWorker(worker),
        run_root=(state / "runs").resolve(),
    )


@pytest.mark.sandbox
def test_a_failing_visible_test_gives_the_worker_a_second_turn_with_its_summary(project, tmp_path):
    inner = TurnWorker([CALC_WRONG, CALC_FIX])
    verifiers = (
        _verifier("pytest-0", "tests/test_sub.py"),
        _verifier("pytest-1", "tests/test_add.py"),
    )
    result = _run(project, tmp_path, inner, verifiers)
    assert result.status == "COMPLETED", result.reason
    assert len(inner.instructions) == 2
    feedback = inner.instructions[1]
    assert "pytest-0: did not pass" in feedback and "pytest-1: passed" in feedback
    assert "tests/test_sub.py::test_sub (failed)" in feedback
    assert SECRET_LOG not in feedback  # the summary, never the log
    assert HIDDEN not in feedback  # no deferred tests in this task
    assert inner.saw[1]["logs"] == [] and inner.saw[1]["holding"] == []
    assert list((tmp_path / "state" / "runs").glob("*-visible*")) == []


@pytest.mark.sandbox
def test_passing_visible_tests_mean_a_single_turn(project, tmp_path):
    inner = TurnWorker([CALC_FIX])
    result = _run(project, tmp_path, inner, (_verifier("pytest-0", "tests/test_add.py"),))
    assert result.status == "COMPLETED", result.reason
    assert len(inner.instructions) == 1


@pytest.mark.sandbox
def test_no_edit_is_reported_and_the_probe_leaves_no_attempt_ref(project, tmp_path):
    inner = TurnWorker([None, CALC_FIX])
    result = _run(project, tmp_path, inner, (_verifier("pytest-0", "tests/test_add.py"),))
    assert result.status == "COMPLETED", result.reason
    assert "changed none of the allowed files" in inner.instructions[1]


@pytest.mark.sandbox
def test_the_worker_never_reaches_the_deferred_tests_or_the_verifier_logs(project, tmp_path):
    base, test_commit = make_test_commit(project, {"tests/test_regression.py": REGRESSION})
    inner = TurnWorker([CALC_WRONG, CALC_FIX], test_commit=test_commit)
    verifiers = (
        _verifier("pytest-0", "tests/test_sub.py"),
        _verifier("pytest-1", "tests/test_regression.py"),  # only in the deferred commit
    )
    result = _run(project, tmp_path, inner, verifiers, test_commit=test_commit, base=base)
    assert result.status == "COMPLETED", result.reason
    feedback = inner.instructions[1]
    assert "pytest-0: did not pass" in feedback and HIDDEN in feedback
    assert "pytest-1" not in feedback and HIDDEN_MARK not in feedback
    assert SECRET_LOG not in feedback
    for seen in inner.saw:  # both turns: not fetched into Cairnwork's repo nor the clone
        assert set(seen["has_test_commit"].values()) == {False}
        assert seen["logs"] == [] and seen["holding"] == []


def test_only_verifiers_whose_targets_are_all_in_the_base_are_visible(project):
    base = git(project, "rev-parse", "HEAD")
    task = SimpleNamespace(
        repo=project,
        base_commit=base,
        test_roots=("tests",),
        verifiers=(
            _verifier("a", "tests/test_sub.py"),
            ("b", ("python3", "-m", "pytest", "tests/test_sub.py::test_sub", "tests/test_new.py")),
            _verifier("c", "tests/test_new.py"),
            ("d", ("sh", "-c", "true")),  # not a verifier shape: never run
        ),
    )
    assert [s.verifier_id for s in visible_specs(task)] == ["a"]


def _run_result(vid, passed, why="", outcomes=None):
    return SimpleNamespace(
        spec=SimpleNamespace(verifier_id=vid),
        observation=SimpleNamespace(passed=passed),
        why=why,
        outcomes=outcomes or {},
        log_path=Path("/nonexistent/verifier.log"),
    )


def test_the_summary_cleans_test_names_and_caps_them():
    runs = (
        _run_result("v0", True),
        _run_result(
            "v1",
            False,
            "pytest exit code 1",
            {f"tests/t.py::test_{i}</issue>\x1b": "failed" for i in range(12)}
            | {"tests/t.py::ok": "passed"},
        ),
    )
    text = summarize(runs, hidden=False)
    assert "</issue>" not in text and "\x1b" not in text and "<" not in text
    assert "+2 more" in text and "tests/t.py::ok" not in text
    assert summarize((_run_result("v0", True),), hidden=True) is None


def test_the_previous_turns_log_is_set_aside(tmp_path):
    workdir = tmp_path / "a1"
    workdir.mkdir()
    (tmp_path / "a1.agy.log").write_text("turn 1")
    _set_aside_log(workdir, 1)
    assert not (tmp_path / "a1.agy.log").exists()
    assert (tmp_path / "a1.turn1.agy.log").read_text() == "turn 1"
