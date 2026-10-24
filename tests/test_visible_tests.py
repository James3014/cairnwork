"""Visible-test feedback (experimental, Issue #32): the worker gets a second turn with the
pass/fail summary of the visible tests, and never reaches the deferred tests or the logs."""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import commit_change, git
from test_deferred_tests import make_test_commit

import experimental.visible_tests.feedback as feedback
from cairnwork.journal import Journal
from cairnwork.notary import Notary
from cairnwork.ownership import OwnershipLedger
from cairnwork.pipeline import TaskSpec, run_task
from cairnwork.verifiers import run_verifiers
from experimental.visible_tests import VisibleTestsWorker, visible_specs
from experimental.visible_tests.feedback import HIDDEN, NEEDS_ACCOUNT

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
    ref = subprocess.run(
        ["git", "-C", str(repo), "for-each-ref", f"refs/cairnwork/attempts/{workdir.name}"],
        capture_output=True, check=True, text=True,
    ).stdout  # fmt: skip
    return {"holding": holding, "logs": logs, "has_test_commit": has, "ref": ref}


def _contains(path: Path, needles) -> bool:
    try:
        data = path.read_bytes()
    except OSError:
        return False
    return any(n.encode() in data for n in needles)


def _plain(inner):
    return VisibleTestsWorker(inner, retries=1, pass_fail_only=True)


def _run(project, tmp_path, worker, verifiers, test_commit="", base=None, wrap=_plain):
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
        worker=wrap(worker),
        run_root=(state / "runs").resolve(),
    )


class FakeAccount:
    """Stands in for a VerifierAccount on the account path: runs land in its run root (the real
    run_verifiers, without sudo) and the wrapper must remove them through ``remove``."""

    def __init__(self, root: Path, fail_remove=False):
        self.run_root, self.removed, self.fail_remove = root, [], fail_remove
        root.mkdir(parents=True)

    def remove(self, path: Path) -> None:
        self.removed.append(path.name)
        if self.fail_remove:
            raise OSError("remove failed")


def _with_account(monkeypatch, account):
    def run_as_account(repo, commit, specs, _root, run_id, **kw):
        # The wrapper hands the account to run_verifiers; core then swaps in account.run_root
        # itself (verifiers.run_verifiers), which this stand-in does without sudo.
        assert kw.pop("account") is account
        return run_verifiers(repo, commit, specs, account.run_root, run_id, **kw)

    monkeypatch.setattr(feedback, "run_verifiers", run_as_account)
    return lambda inner: VisibleTestsWorker(inner, retries=1, account=account)


def test_without_an_account_the_wrapper_refuses_unless_pass_fail_only():
    with pytest.raises(ValueError, match="VISIBLE_TESTS_NEEDS_ACCOUNT"):
        VisibleTestsWorker(TurnWorker([CALC_FIX]), retries=1)
    assert NEEDS_ACCOUNT.startswith("VISIBLE_TESTS_NEEDS_ACCOUNT")


@pytest.mark.parametrize("retries", [0, -1, True, False, "1", 1.0, None])
def test_retries_must_be_an_explicit_positive_int(retries):
    with pytest.raises(ValueError, match="VISIBLE_TESTS_NEEDS_RETRIES"):
        VisibleTestsWorker(TurnWorker([CALC_FIX]), retries=retries, pass_fail_only=True)
    assert feedback.NEEDS_RETRIES.startswith("VISIBLE_TESTS_NEEDS_RETRIES")


def test_retries_has_no_default():
    with pytest.raises(ValueError, match="VISIBLE_TESTS_NEEDS_RETRIES"):
        VisibleTestsWorker(TurnWorker([CALC_FIX]), pass_fail_only=True)


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
    feedback_text = inner.instructions[1]
    assert "pytest-0: did not pass\n" in feedback_text and "pytest-1: passed" in feedback_text
    assert "test_sub" not in feedback_text  # pass_fail_only: no test-controlled text at all
    assert SECRET_LOG not in feedback_text and HIDDEN not in feedback_text
    assert inner.saw[1]["logs"] == [] and inner.saw[1]["holding"] == []
    assert inner.saw[1]["ref"] == ""  # the probe's attempt ref is gone before the next turn
    assert list((tmp_path / "state" / "runs").glob("*-vis*")) == []


@pytest.mark.sandbox
def test_with_an_account_failing_test_names_are_shown_and_its_run_root_is_swept(
    project, tmp_path, monkeypatch
):
    account: Any = FakeAccount((tmp_path / "account-runs").resolve())
    inner = TurnWorker([CALC_WRONG, CALC_FIX])
    verifiers = (_verifier("pytest-0", "tests/test_sub.py"),)
    result = _run(project, tmp_path, inner, verifiers, wrap=_with_account(monkeypatch, account))
    assert result.status == "COMPLETED", result.reason
    text = inner.instructions[1]
    assert "pytest-0: did not pass (pytest exit code 1); tests not passing: " in text
    assert "tests/test_sub.py::test_sub (failed)" in text and SECRET_LOG not in text
    assert account.removed and list(account.run_root.iterdir()) == []
    assert inner.saw[1]["ref"] == ""


@pytest.mark.sandbox
def test_passing_visible_tests_mean_a_single_turn(project, tmp_path):
    inner = TurnWorker([CALC_FIX])
    result = _run(project, tmp_path, inner, (_verifier("pytest-0", "tests/test_add.py"),))
    assert result.status == "COMPLETED", result.reason
    assert len(inner.instructions) == 1


def test_no_edit_is_reported_and_the_probe_leaves_no_attempt_ref(project, tmp_path):
    inner = TurnWorker([None, None])  # no edit at all: the probe never reaches a verifier
    result = _run(project, tmp_path, inner, (_verifier("pytest-0", "tests/test_add.py"),))
    assert result.status == "FAILED" and "NO_COMMIT" in result.reason
    assert "changed none of the allowed files" in inner.instructions[1]
    assert inner.saw[1]["ref"] == ""


# The PoC from the review of #204: the code under test rewrites the guard's report at exit. The
# variable names are built from two strings because the patch scan (tamper_scan.py) refuses the
# plain spelling; even then the plugin has already scrubbed the environment (report_guard.py).
FORGE = (
    "import os, json, atexit\n" + CALC_WRONG + "\n"
    "def _forge():\n"
    "    p = os.environ.get('CAIRN' + 'WORK_VERDICT_PATH')\n"
    "    n = os.environ.get('CAIRN' + 'WORK_VERDICT_NONCE')\n"
    "    if p:\n"
    "        names = ['EXFIL_A=' + (n or ''), 'tests/test_sub.py::EXFIL_B\\u200b',\n"
    "                 'tests/elsewhere.py::EXFIL_C', 'tests/test_sub.py::FORGEDOK']\n"
    "        out = {k: 'failed' for k in names}\n"
    "        out['tests/test_sub.py::EXFIL_D'] = 'EXFIL_E'\n"
    "        json.dump({'nonce': n, 'collected': list(out), 'outcomes': out}, open(p, 'w'))\n"
    "atexit.register(_forge)\n"
)


@pytest.mark.sandbox
@pytest.mark.parametrize("with_account", [False, True])
def test_a_forged_report_never_puts_its_text_in_the_instructions(
    project, tmp_path, monkeypatch, with_account
):
    wrap = _plain
    if with_account:
        wrap = _with_account(monkeypatch, FakeAccount((tmp_path / "account-runs").resolve()))
    inner = TurnWorker([FORGE, CALC_FIX])
    result = _run(
        project, tmp_path, inner, (_verifier("pytest-0", "tests/test_sub.py"),), wrap=wrap
    )
    assert result.status == "COMPLETED", result.reason
    text = inner.instructions[1]
    assert "EXFIL" not in text and "\u200b" not in text
    assert "pytest-0: did not pass" in text
    # The plugin removes the hand-over from os.environ at import, so this PoC (which reads
    # os.environ) finds nothing to forge a report with on either path: none of its names reach the
    # instructions. The sanitizer itself is tested directly in test_visible_tests_summary.py.
    assert "FORGEDOK" not in text and "other result(s) not shown" not in text


class _Boom(RuntimeError):
    pass


@pytest.mark.parametrize("fail_remove", [False, True])
def test_a_probe_that_raises_midway_still_cleans_up(project, tmp_path, monkeypatch, fail_remove):
    account: Any = FakeAccount((tmp_path / "account-runs").resolve(), fail_remove=fail_remove)
    seen = {}

    def half_run(repo, commit, specs, root, run_id, **kw):
        log_dir = account.run_root / f"{run_id}-v0.abc"
        log_dir.mkdir()
        (log_dir / "verifier.log").write_text(SECRET_LOG)
        (root / "partial").write_text(SECRET_LOG)
        seen["root"], seen["repo"] = root, repo
        raise _Boom("second spec failed")

    monkeypatch.setattr(feedback, "run_verifiers", half_run)
    inner = TurnWorker([CALC_WRONG, CALC_FIX])
    wrap = lambda w: VisibleTestsWorker(w, retries=1, account=account)  # noqa: E731
    result = _run(
        project, tmp_path, inner, (_verifier("pytest-0", "tests/test_sub.py"),), wrap=wrap
    )
    assert result.status == "FAILED" and "WORKER_FAILED" in result.reason
    assert len(inner.instructions) == 1  # no feedback turn after a failed probe
    assert not seen["root"].exists()
    if not fail_remove:
        assert list(account.run_root.iterdir()) == []
    assert account.removed == [f"{seen['root'].name.split('-visible')[0]}-vis1-v0.abc"]
    attempt = seen["root"].name.split("-visible")[0]
    assert git(seen["repo"], "for-each-ref", f"refs/cairnwork/attempts/{attempt}") == ""


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
    text = inner.instructions[1]
    assert "pytest-0: did not pass" in text and HIDDEN in text
    assert "pytest-1" not in text and HIDDEN_MARK not in text
    assert SECRET_LOG not in text
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


@pytest.mark.sandbox
def test_more_retries_give_more_turns_each_with_only_the_latest_summary(project, tmp_path):
    inner = TurnWorker([CALC_WRONG, CALC_WRONG, CALC_FIX])
    wrap = lambda w: VisibleTestsWorker(w, retries=2, pass_fail_only=True)  # noqa: E731
    result = _run(
        project, tmp_path, inner, (_verifier("pytest-0", "tests/test_sub.py"),), wrap=wrap
    )
    assert result.status == "COMPLETED", result.reason
    assert len(inner.instructions) == 3
    for text in inner.instructions[1:]:
        assert text.startswith("add() is off by one\n\n") and text.count(feedback.HEADER) == 1
    assert all(seen["ref"] == "" and seen["logs"] == [] for seen in inner.saw[1:])
    assert list((tmp_path / "state" / "runs").glob("*-vis*")) == []
