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
