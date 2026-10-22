"""Crash drills: kill a real Cairnwork process at each phase, then recover."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import commit_change

from cairnwork import runlock
from cairnwork.journal import Journal
from cairnwork.notary import Notary
from cairnwork.ownership import OwnershipLedger
from cairnwork.recovery import recover

DRIVER = Path(__file__).with_name("crash_driver.py")
GOOD = "def add(a, b):\n    return a + b\n"
BAD = "def add(a, b):\n    return a - b\n"


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


def stores(state):
    return (
        OwnershipLedger(state / "own.sqlite"),
        Notary(state / "notary.sqlite"),
        Journal(state / "journal.sqlite"),
    )


def crash(project, state, fault, fix=GOOD):
    env = {**os.environ, "CAIRNWORK_FAULT_AFTER": fault}
    done = subprocess.run(
        [sys.executable, str(DRIVER), str(project), str(state), fix],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert done.returncode == 137, done.stderr[-2000:]


def settle(state):
    ledger, notary, journal = stores(state)
    return recover(ledger=ledger, notary=notary, journal=journal, run_root=state / "runs"), ledger


@pytest.mark.parametrize("fault", ["DISPATCHED", "WORKER_STARTED", "CANDIDATE"])
def test_crash_before_verification_ends_failed(project, tmp_path, fault):
    crash(project, tmp_path, fault)
    decisions, ledger = settle(tmp_path)
    previous = "BOUND" if fault == "DISPATCHED" else "DISPATCHED"  # journal is written first
    assert [(d.previous_status, d.new_status, d.reason) for d in decisions] == [
        (previous, "FAILED", f"INTERRUPTED_AFTER_{fault}")
    ]
    assert ledger.get(task_id=decisions[0].task_id).status == "FAILED"
    assert not (tmp_path / "runs" / decisions[0].attempt_id).exists()


@pytest.mark.sandbox
@pytest.mark.parametrize("fault", ["VERIFYING", "NOTARIZING"])
def test_crash_before_core_records_a_verdict_ends_failed(project, tmp_path, fault):
    crash(project, tmp_path, fault)
    decisions, _ = settle(tmp_path)
    assert [(d.new_status, d.reason) for d in decisions] == [
        ("FAILED", f"INTERRUPTED_AFTER_{fault}")
    ]


@pytest.mark.sandbox
def test_crash_after_core_verified_is_completed(project, tmp_path):
    crash(project, tmp_path, "FINISHED", GOOD)
    decisions, ledger = settle(tmp_path)
    assert [(d.new_status, d.reason) for d in decisions] == [
        ("COMPLETED", "CORE_VERIFIED_BEFORE_CRASH")
    ]
    assert ledger.get(task_id=decisions[0].task_id).status == "COMPLETED"


@pytest.mark.sandbox
def test_crash_after_core_rejected_is_failed(project, tmp_path):
    crash(project, tmp_path, "FINISHED", BAD)
    decisions, _ = settle(tmp_path)
    assert [(d.new_status, d.reason) for d in decisions] == [
        ("FAILED", "INTERRUPTED_AFTER_FINISHED")
    ]


def test_recovery_is_idempotent(project, tmp_path):
    crash(project, tmp_path, "WORKER_STARTED")
    first, _ = settle(tmp_path)
    second, _ = settle(tmp_path)
    assert len(first) == 1 and second == []


def test_task_bound_without_a_journal_entry_ends_failed(project, tmp_path):
    ledger, _, _ = stores(tmp_path)
    ledger.bind("k9", {"x": 1}, "cairnwork", "card-9")
    decisions, ledger = settle(tmp_path)
    assert [(d.previous_status, d.new_status, d.reason) for d in decisions] == [
        ("BOUND", "FAILED", "INTERRUPTED_BEFORE_DISPATCH")
    ]


def test_recovery_leaves_another_owners_task_untouched(tmp_path):
    ledger, _, _ = stores(tmp_path)
    task = ledger.bind("k8", {"x": 1}, "nexus-new", "card-8").record.task_id
    decisions, ledger = settle(tmp_path)
    assert [(d.task_id, d.new_status, d.reason) for d in decisions] == [
        (task, "BOUND", "FOREIGN_OWNER")
    ]
    assert ledger.get(task_id=task).status == "BOUND"


def test_recovery_leaves_another_owners_dispatched_task_untouched(tmp_path):
    ledger, _, _ = stores(tmp_path)
    task = ledger.bind("k9", {"x": 1}, "nexus-new", "card-9").record.task_id
    ledger.set_status(task, "DISPATCHED")
    decisions, ledger = settle(tmp_path)
    assert [(d.task_id, d.previous_status, d.new_status, d.reason) for d in decisions] == [
        (task, "DISPATCHED", "DISPATCHED", "FOREIGN_OWNER")
    ]
    assert ledger.get(task_id=task).status == "DISPATCHED"


def test_recovery_refuses_while_a_run_holds_the_lock(project, tmp_path):
    proc = subprocess.Popen(
        [sys.executable, str(DRIVER), str(project), str(tmp_path), GOOD, "orphan"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 60
        while not (tmp_path / "worker-running").exists():
            assert time.monotonic() < deadline and proc.poll() is None
            time.sleep(0.1)
        with pytest.raises(runlock.RunLockBusy):
            settle(tmp_path)
    finally:
        proc.kill()
        proc.wait()


def test_sigkill_mid_worker_kills_the_orphan_and_fails_the_task(project, tmp_path):
    proc = subprocess.Popen(
        [sys.executable, str(DRIVER), str(project), str(tmp_path), GOOD, "orphan"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 60
    while not (tmp_path / "worker-running").exists():
        assert time.monotonic() < deadline and proc.poll() is None
        time.sleep(0.1)
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait()
    decisions, _ = settle(tmp_path)
    assert [(d.new_status, d.reason) for d in decisions] == [
        ("FAILED", "INTERRUPTED_AFTER_WORKER_STARTED")
    ]
    assert decisions[0].killed_processes == 1
    time.sleep(5)
    assert not (tmp_path / "orphan-wrote").exists()


def test_one_bad_record_does_not_stop_the_others(project, tmp_path, monkeypatch):
    ledger, _, _ = stores(tmp_path)
    ledger.bind("k8", {"x": 1}, "cairnwork", "card-8")
    ledger.bind("k9", {"x": 2}, "cairnwork", "card-9")
    import cairnwork.recovery as recovery

    real = recovery._settle
    calls = []

    def flaky(record, *args):
        calls.append(record.task_id)
        if len(calls) == 1:
            raise RuntimeError("corrupt")
        return real(record, *args)

    monkeypatch.setattr(recovery, "_settle", flaky)
    decisions, _ = settle(tmp_path)
    reasons = sorted(d.reason.split(":")[0] for d in decisions)
    assert reasons == ["INTERRUPTED_BEFORE_DISPATCH", "RECOVERY_ERROR"]


def test_sweep_matches_the_whole_marker_only():
    import secrets

    from cairnwork.confined import sweep_marked

    value = "a" + secrets.token_hex(8)
    longer = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env={**os.environ, "CW_SWEEP_TEST": value + "0"},
    )
    exact = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env={**os.environ, "CW_SWEEP_TEST": value},
    )
    try:
        time.sleep(0.5)
        assert sweep_marked("CW_SWEEP_TEST", value) == 1
        time.sleep(0.5)
        assert exact.poll() is not None
        assert longer.poll() is None
    finally:
        for proc in (longer, exact):
            if proc.poll() is None:
                proc.kill()
            proc.wait()


@pytest.mark.sandbox
def test_run_that_decided_failed_stays_failed_even_if_core_verified(project, tmp_path):
    crash(project, tmp_path, "FINISHED", GOOD)
    ledger, notary, journal = stores(tmp_path)
    task_id = ledger.in_flight()[0].task_id
    attempt = journal.entries(task_id)[-1].attempt_id
    journal.record(task_id, attempt, "FINISHED", "FAILED")  # e.g. INFRA_ERROR after notarize
    decisions, _ = settle(tmp_path)
    assert [(d.new_status, d.reason) for d in decisions] == [
        ("FAILED", "INTERRUPTED_AFTER_FINISHED")
    ]


def test_recovery_with_another_run_root_still_waits_for_the_live_run(project, tmp_path):
    proc = subprocess.Popen(
        [sys.executable, str(DRIVER), str(project), str(tmp_path), GOOD, "orphan"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 60
        while not (tmp_path / "worker-running").exists():
            assert time.monotonic() < deadline and proc.poll() is None
            time.sleep(0.1)
        ledger, notary, journal = stores(tmp_path)
        with pytest.raises(runlock.RunLockBusy):
            recover(ledger=ledger, notary=notary, journal=journal, run_root=tmp_path / "other")
        assert ledger.in_flight()[0].status == "DISPATCHED"
    finally:
        proc.kill()
        proc.wait()


def test_dispatched_task_without_journal_is_reported_not_failed(project, tmp_path):
    ledger, _, _ = stores(tmp_path)
    task = ledger.bind("k7", {"x": 1}, "cairnwork", "card-7").record
    ledger.set_status(task.task_id, "DISPATCHED")
    decisions, ledger = settle(tmp_path)
    assert decisions[0].reason.startswith("RECOVERY_ERROR")
    assert ledger.get(task_id=task.task_id).status == "DISPATCHED"


@pytest.mark.sandbox
def test_sigkill_during_verification_kills_the_verifier_and_cleans_up(repo, tmp_path):
    commit_change(
        repo,
        {
            "src/calc.py": "def add(a, b):\n    return a + b + 1\n",
            "tests/conftest.py": (
                "import sys, pathlib\n"
                "sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / 'src'))\n"
            ),
            "tests/test_calc.py": (
                "import time\nfrom calc import add\n"
                "def test_add():\n    time.sleep(40)\n    assert add(2, 3) == 5\n"
            ),
        },
    )
    proc = subprocess.Popen(
        [sys.executable, str(DRIVER), str(repo), str(tmp_path), GOOD],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 60
    journal = Journal(tmp_path / "journal.sqlite")
    while True:
        assert time.monotonic() < deadline and proc.poll() is None
        ledger = OwnershipLedger(tmp_path / "own.sqlite")
        flight = ledger.in_flight()
        if flight and any(e.phase == "VERIFYING" for e in journal.entries(flight[0].task_id)):
            break
        time.sleep(0.2)
    time.sleep(3)  # let the sandboxed pytest start
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait()
    decisions, _ = settle(tmp_path)
    assert [(d.new_status, d.reason) for d in decisions] == [
        ("FAILED", "INTERRUPTED_AFTER_VERIFYING")
    ]
    assert decisions[0].killed_processes >= 1
    attempt = decisions[0].attempt_id
    runs = tmp_path / "runs"
    assert not (runs / f"{attempt}-v-v0").exists()
    assert all(not (p / "run").exists() for p in runs.glob(f"{attempt}-v-v0.*"))


def test_leftover_cleanup_ignores_an_attempt_id_that_is_not_a_name(tmp_path):
    from cairnwork.recovery import _remove_leftovers

    victim = tmp_path / "keep.index"
    victim.write_text("x")
    (tmp_path / "runs").mkdir()
    _remove_leftovers(tmp_path / "runs", "../keep")
    assert victim.exists()


def test_leftover_cleanup_removes_whole_base_test_runs(tmp_path):
    from cairnwork.recovery import _remove_leftovers

    base_run = tmp_path / "a1-b-v0.abc"
    verify_run = tmp_path / "a1-v-v0.def"
    for private in (base_run, verify_run):
        (private / "run").mkdir(parents=True)
        (private / "verifier.log").write_text("log")
    _remove_leftovers(tmp_path, "a1")
    assert not base_run.exists()
    assert (verify_run / "verifier.log").exists() and not (verify_run / "run").exists()


def test_leftover_cleanup_removes_verifier_work_directories(tmp_path):
    """#40 layout: <attempt>-v-v0.work/<attempt>-v-v0 is the checkout; recovery removes it
    whole and still keeps the verifier log next to it."""
    from cairnwork.recovery import _remove_leftovers

    for kind in ("v", "b"):
        work = tmp_path / f"a1-{kind}-v0.work"
        (work / f"a1-{kind}-v0" / "tests").mkdir(parents=True)
        (work / "nexus-runtime-targets").mkdir()
    verify_run = tmp_path / "a1-v-v0.def"
    (verify_run / "run").mkdir(parents=True)
    (verify_run / "verifier.log").write_text("log")
    _remove_leftovers(tmp_path, "a1")
    assert not list(tmp_path.glob("*.work"))
    assert (verify_run / "verifier.log").exists() and not (verify_run / "run").exists()
