"""#173: the hidden-test run directories do not survive a crash, and a failed cleanup is loud.

Real processes: a run is SIGKILLed during the P2P step (-q candidate run, -r re-run), then
recovery runs. A removal that fails is reported (LEFTOVER_RUN_DIRS), never counted as clean.
"""

import json
import os
import signal
import sqlite3
import stat
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

from cairnwork.recovery import _leftover_names, _remove_inside, recover
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
        assert decision.detail == f"LEFTOVER_RUN_DIRS: 2: {attempt}, {stuck.name}"  # names only
        assert stores(tmp_path)[2].entries(task)[-1].phase == "CANDIDATE"  # no journal row added
        assert stores(tmp_path)[0].get(task_id=task).status == "FAILED"
        assert _recover(tmp_path) == []
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
    assert decision.detail == f"LEFTOVER_RUN_DIRS: 2: {attempt}-r-v0, {attempt}-v-v0.x1/run"


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
    log = tmp_path / "leftovers.jsonl"
    ledger, notary, journal = stores(tmp_path)
    (decision,) = recover(
        ledger=ledger, notary=notary, journal=journal, run_root=tmp_path / "runs",
        leftovers_log=log,
    )  # fmt: skip
    assert decision.detail == "" and not log.exists()
    assert stores(tmp_path)[2].entries(task)[-1].phase != "FINISHED"


@pytest.mark.sandbox
def test_a_failed_run_with_leftovers_stays_failed_however_often_recovery_runs(
    crash_project, tmp_path
):
    """A leftover note must not look like the run's own FINISHED row (it once could flip FAILED)."""
    crash(crash_project, tmp_path, "FINISHED", GOOD)
    ledger, notary, journal = stores(tmp_path)
    task = ledger.in_flight()[0].task_id
    attempt = journal.entries(task)[-1].attempt_id
    journal.record(task, attempt, "FINISHED", "FAILED")  # the run's own decision, Core verified
    before = journal.entries(task)
    log = tmp_path / "state" / "leftovers.jsonl"
    log.parent.mkdir()
    stuck = _stuck_dir(tmp_path, attempt, "q")
    try:
        results = []
        for _ in range(2):
            results.append(
                recover(
                    ledger=ledger,
                    notary=notary,
                    journal=journal,
                    run_root=tmp_path / "runs",
                    leftovers_log=log,
                )  # fmt: skip
            )
            if len(results) == 1:  # put the task back in flight: a later recovery re-decides it
                with sqlite3.connect(ledger.path) as conn:
                    conn.execute(
                        "UPDATE task_ownership SET status='DISPATCHED' WHERE task_id=?", (task,)
                    )
    finally:
        (tmp_path / "runs").chmod(0o700)
    for (decision,) in results:
        assert decision.new_status == "FAILED"
        assert "LEFTOVER_RUN_DIRS" in decision.detail and stuck.name in decision.detail
    assert journal.entries(task) == before
    assert ledger.get(task_id=task).status == "FAILED"
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(r["task_id"], r["attempt_id"], r["count"]) for r in rows] == [(task, attempt, 1)] * 2
    assert stuck.name in rows[0]["names"] and "time" in rows[0]
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_leftover_names_are_sanitised_and_bounded(crash_project, tmp_path):
    _, attempt = _crashed_attempt(crash_project, tmp_path)
    runs = tmp_path / "runs"
    for i in range(8):
        (runs / f"{attempt}-q-v{i}.x\x1b[31m{'y' * 200}").mkdir()
    runs.chmod(0o500)
    try:
        (decision,) = _recover(tmp_path)
    finally:
        runs.chmod(0o700)
    assert "\x1b" not in decision.detail and "[" not in decision.detail
    assert "+" in decision.detail and "more" in decision.detail  # at most five names shown
    assert max(map(len, decision.detail.split(", "))) < 160


def test_a_symlink_is_unlinked_never_followed(crash_project, tmp_path):
    _, attempt = _crashed_attempt(crash_project, tmp_path)
    outside = tmp_path / "outside"
    (outside / "keep").mkdir(parents=True)
    (outside / "keep" / "f").write_text("x")
    runs = tmp_path / "runs"
    (runs / f"{attempt}-q-v0.x").symlink_to(outside)  # a private run dir that is a link
    (runs / f"{attempt}-p-v0").symlink_to(outside)  # a clone that is a link
    (runs / f"{attempt}-b-v0.work").symlink_to(outside)  # a checkout that is a link
    verify = runs / f"{attempt}-v-v0.x"
    verify.mkdir()
    (verify / "run").symlink_to(outside)  # a verify scratch dir that is a link
    (decision,) = _recover(tmp_path)
    assert decision.detail == ""
    assert (outside / "keep" / "f").read_text() == "x"
    assert not [p for p in runs.glob(f"{attempt}-*") if p.is_symlink()]
    assert not (verify / "run").exists() and not (verify / "run").is_symlink()
    assert _leftover_names(runs, attempt) == []


def test_remove_inside_refuses_a_target_that_resolves_outside(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    (outside / "d").mkdir(parents=True)
    (root / "via").symlink_to(outside)  # a parent component that is a link
    _remove_inside(root, root / "via" / "d")
    assert (outside / "d").is_dir()
    _remove_inside(root, root)  # the root itself is not strictly inside itself
    assert root.is_dir()


def test_a_dangling_symlink_that_cannot_be_removed_is_reported(crash_project, tmp_path):
    _, attempt = _crashed_attempt(crash_project, tmp_path)
    runs = tmp_path / "runs"
    (runs / f"{attempt}-r-v0").symlink_to(tmp_path / "nowhere")
    runs.chmod(0o500)
    try:
        (decision,) = _recover(tmp_path)
    finally:
        runs.chmod(0o700)
    assert f"{attempt}-r-v0" in decision.detail


def test_both_the_state_and_the_dispatched_run_root_are_cleaned(crash_project, tmp_path):
    task, attempt = _crashed_attempt(crash_project, tmp_path)
    ledger, notary, journal = stores(tmp_path)
    other = tmp_path / "other-runs"
    (other / f"{attempt}-q-v0").mkdir(parents=True)
    (tmp_path / "runs" / f"{attempt}-r-v0").mkdir()
    assert str(tmp_path / "runs") in [e.detail for e in journal.entries(task)]
    (decision,) = recover(  # the journal says tmp/runs; the state says other-runs
        ledger=ledger, notary=notary, journal=journal, run_root=other,
    )  # fmt: skip
    assert decision.detail == ""
    assert not list(other.iterdir()) and not list((tmp_path / "runs").glob(f"{attempt}-r-*"))


def _route_recover(state):
    done = subprocess.run(
        [sys.executable, "-I", "-m", "cairnwork.route", "--state", str(state), "recover"],
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    return done.returncode, json.loads(done.stdout)


def _routable(crash_project, tmp_path):
    """The crashed state under the file names the route reads."""
    _, attempt = _crashed_attempt(crash_project, tmp_path)
    (tmp_path / "own.sqlite").rename(tmp_path / "ownership.sqlite")
    (tmp_path / "config.json").write_text(json.dumps({"verifier_account": False}))
    tmp_path.chmod(0o700)
    return attempt


def test_route_recover_with_leftovers_counts_them_and_exits_3(crash_project, tmp_path):
    attempt = _routable(crash_project, tmp_path)
    _stuck_dir(tmp_path, attempt, "q")
    try:
        code, out = _route_recover(tmp_path)
    finally:
        (tmp_path / "runs").chmod(0o700)
    assert code == 3 and out["leftover_run_dirs"] == 2
    assert out["decisions"][0]["leftover_run_dirs"] == 2


def test_route_recover_when_clean_counts_zero_and_exits_0(crash_project, tmp_path):
    _routable(crash_project, tmp_path)
    code, out = _route_recover(tmp_path)
    assert code == 0 and out["leftover_run_dirs"] == 0


def test_a_symlinked_leftovers_log_is_refused(crash_project, tmp_path):
    _, attempt = _crashed_attempt(crash_project, tmp_path)
    target = tmp_path / "elsewhere.jsonl"
    log = tmp_path / "leftovers.jsonl"
    log.symlink_to(target)
    _stuck_dir(tmp_path, attempt, "q")
    ledger, notary, journal = stores(tmp_path)
    try:
        (decision,) = recover(
            ledger=ledger, notary=notary, journal=journal, run_root=tmp_path / "runs",
            leftovers_log=log,
        )  # fmt: skip
    finally:
        (tmp_path / "runs").chmod(0o700)
    assert not target.exists()  # O_NOFOLLOW: the link was not followed or created through
    assert decision.detail.endswith("(leftover log write failed)")
    assert decision.leftover_run_dirs == 2
