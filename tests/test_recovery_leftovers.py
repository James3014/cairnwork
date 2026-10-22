"""#173: the hidden-test run directories do not survive a crash, and a failed cleanup is loud.

Real processes: a run is SIGKILLed during the P2P step (-q candidate run, -r re-run), then
recovery runs. A removal that fails is reported (LEFTOVER_RUN_DIRS), never counted as clean.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import commit_change
from test_deferred_tests import REGRESSION, make_test_commit
from test_recovery import (
    GOOD,
    crash,
    project,  # noqa: F401 - fixture, used through crash_project
    stores,
)
from test_recovery_account_cleanup import _FakeAccount

from cairnwork.recovery import _leftover_names, recover
from cairnwork.verifier_account import VerifierAccount

DRIVER = Path(__file__).with_name("p2p_crash_driver.py")
SLOW = "import time, calc\ndef test_slow():\n    if calc.SLOW:\n        time.sleep(120)\n"
CALC = "SLOW = False\ndef add(a, b):\n    return a + b + 1\n"
HIDDEN_TAGS = ("p", "q", "r", "v", "b")


@pytest.fixture
def crash_project(request):
    return request.getfixturevalue("project")


@pytest.fixture
def p2p_project(repo):
    commit_change(
        repo,
        {
            "src/calc.py": CALC,
            "tests/conftest.py": (
                "import sys, pathlib\n"
                "sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / 'src'))\n"
            ),
            "tests/test_slow.py": SLOW,
        },
    )
    return repo, *make_test_commit(repo, {"tests/test_regression.py": REGRESSION})


def _hidden(root: Path, attempt: str = "*") -> list[Path]:
    """Run directories that can hold hidden tests, plus a verify run's scratch ``run/``."""
    found = []
    for tag in HIDDEN_TAGS:
        for path in root.glob(f"{attempt}-{tag}-v*"):
            if tag == "v" and "." in path.name and path.suffix != ".work":
                path = path / "run"
                if not path.exists():
                    continue
            found.append(path)
    return found


def _driver(p2p_project, state, step, *account):
    repo, base, tests = p2p_project
    with open(Path(state) / "driver.err", "w") as err:  # the child keeps its own copy
        return subprocess.Popen(
            [sys.executable, str(DRIVER), str(repo), str(state), base, tests, step, *account],
            stdout=subprocess.DEVNULL,
            stderr=err,
        )


def _kill_in_p2p(p2p_project, state, step, run_root, *account):
    before = {p.name for p in run_root.glob(f"*-{step}-v*")} if run_root.is_dir() else set()
    proc = _driver(p2p_project, state, step, *account)
    deadline = time.monotonic() + 240
    while not ({p.name for p in run_root.glob(f"*-{step}-v*")} - before):
        assert proc.poll() is None, (state / "driver.err").read_text()[-2000:]
        assert time.monotonic() < deadline
        time.sleep(0.2)
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait()


def _recover(state, account=None):
    ledger, notary, journal = stores(state)
    return recover(
        ledger=ledger, notary=notary, journal=journal, run_root=state / "runs",
        verifier_account=account,
    )  # fmt: skip


def _assert_clean_and_logs_kept(root: Path, attempt: str):
    assert _hidden(root, attempt) == []
    assert _leftover_names(root, attempt) == []
    assert list(root.glob(f"{attempt}-v-v*.*/verifier.log")), "the verify log must survive"


@pytest.mark.sandbox
def test_a_normal_run_leaves_nothing_but_the_logs(p2p_project, tmp_path):
    proc = _driver(p2p_project, tmp_path, "n")
    assert proc.wait(timeout=300) == 0, (tmp_path / "driver.err").read_text()[-2000:]
    runs = tmp_path / "runs"
    names = [p.name for p in runs.iterdir()]
    # only the private log dirs (verify and P2P evidence) are kept: no clone, checkout or scratch
    assert names and all("." in n and not n.endswith(".work") for n in names)
    assert not list(runs.glob("*.work")) and not list(runs.glob("*/tests"))
    assert list(runs.glob("*-v-v*.*/verifier.log"))


@pytest.mark.sandbox
@pytest.mark.parametrize("step", ["q", "r"])
def test_sigkill_during_p2p_leaves_no_run_directory_and_keeps_the_logs(p2p_project, tmp_path, step):
    _kill_in_p2p(p2p_project, tmp_path, step, tmp_path / "runs")
    assert _hidden(tmp_path / "runs"), "the drill must have left something to clean"
    (decision,) = _recover(tmp_path)
    assert decision.new_status == "FAILED" and decision.detail == ""
    _assert_clean_and_logs_kept(tmp_path / "runs", decision.attempt_id)


@pytest.mark.sandbox
@pytest.mark.verifier_account
@pytest.mark.parametrize("step", ["q", "r"])
def test_sigkill_during_p2p_cleans_the_verifier_accounts_run_directories(
    p2p_project, tmp_path, step
):
    account = VerifierAccount()
    _kill_in_p2p(p2p_project, tmp_path, step, account.run_root, "account")
    (decision,) = _recover(tmp_path, account)
    assert decision.new_status == "FAILED" and decision.detail == ""
    _assert_clean_and_logs_kept(account.run_root, decision.attempt_id)
    assert _hidden(tmp_path / "runs", decision.attempt_id) == []
    for path in account.run_root.glob(f"{decision.attempt_id}-*"):
        account.remove(path)  # the kept verify log is the account's: do not leave it behind


def _stuck_dir(tmp_path, attempt, tag):
    """A run directory nobody can remove: its parent, the run root, is read-only."""
    stuck = tmp_path / "runs" / f"{attempt}-{tag}-v0"
    (stuck / "tests").mkdir(parents=True)
    (stuck / "tests" / "test_hidden.py").write_text("x\n")
    (tmp_path / "runs").chmod(0o500)  # nothing can be unlinked from the run root
    return stuck


def _crashed_attempt(crash_project, tmp_path, fault="CANDIDATE"):
    crash(crash_project, tmp_path, fault, GOOD)
    ledger, _, journal = stores(tmp_path)
    task = ledger.in_flight()[0].task_id
    return task, journal.entries(task)[-1].attempt_id


def test_a_removal_that_fails_is_reported_not_called_clean(crash_project, tmp_path):
    task, attempt = _crashed_attempt(crash_project, tmp_path)
    stuck = _stuck_dir(tmp_path, attempt, "q")
    try:
        (decision,) = _recover(tmp_path)
        assert (decision.new_status, decision.reason) == ("FAILED", "INTERRUPTED_AFTER_CANDIDATE")
        assert decision.detail == f"LEFTOVER_RUN_DIRS: 2 ({attempt}, {stuck.name})"  # names only
        last = stores(tmp_path)[2].entries(task)[-1]
        assert (last.phase, last.detail) == ("FINISHED", decision.detail)
        assert stores(tmp_path)[0].get(task_id=task).status == "FAILED"
        assert _recover(tmp_path) == []  # the journal note did not reopen the task
    finally:
        (tmp_path / "runs").chmod(0o700)


def test_a_failed_account_side_removal_is_reported(crash_project, tmp_path):
    _, attempt = _crashed_attempt(crash_project, tmp_path)
    ledger, notary, journal = stores(tmp_path)
    runs = tmp_path / "acct-runs"
    (runs / f"{attempt}-r-v0").mkdir(parents=True)
    (runs / f"{attempt}-v-v0.x1" / "run").mkdir(parents=True)  # verify scratch counts
    (runs / f"{attempt}-v-v0.x1" / "verifier.log").write_text("log")  # evidence does not

    class Stuck(_FakeAccount):
        def remove(self, path):
            self.removed.append(Path(path))  # claims success but removes nothing

    scratch = runs / f"{attempt}-v-v0.x1"
    for locked in (scratch, runs):  # neither the account nor the owner can unlink inside
        locked.chmod(0o500)
    try:
        (decision,) = recover(
            ledger=ledger, notary=notary, journal=journal, run_root=tmp_path / "runs",
            verifier_account=Stuck(runs),  # type: ignore[arg-type]
        )  # fmt: skip
    finally:
        for locked in (runs, scratch):
            locked.chmod(0o700)
    assert decision.detail == f"LEFTOVER_RUN_DIRS: 2 ({attempt}-r-v0, {attempt}-v-v0.x1/run)"


def test_the_owner_finishes_what_the_accounts_remove_could_not(crash_project, tmp_path):
    """The verifier checkout and work dir belong to the owner: the account's rm leaves them."""
    _, attempt = _crashed_attempt(crash_project, tmp_path)
    ledger, notary, journal = stores(tmp_path)
    runs = tmp_path / "acct-runs"
    (runs / f"{attempt}-q-v0.work" / f"{attempt}-q-v0").mkdir(parents=True)

    class Stuck(_FakeAccount):
        def remove(self, path):
            self.removed.append(Path(path))

    (decision,) = recover(
        ledger=ledger, notary=notary, journal=journal, run_root=tmp_path / "runs",
        verifier_account=Stuck(runs),  # type: ignore[arg-type]
    )  # fmt: skip
    assert decision.detail == "" and not list(runs.iterdir())


def test_nothing_remaining_reports_nothing_and_writes_no_note(crash_project, tmp_path):
    task, _ = _crashed_attempt(crash_project, tmp_path, "WORKER_STARTED")
    (decision,) = _recover(tmp_path)
    assert decision.detail == ""
    assert stores(tmp_path)[2].entries(task)[-1].phase != "FINISHED"
