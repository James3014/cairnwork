"""The shared run lock is bounded: a run gives up with RunLockBusy instead of waiting forever
behind a hung recovery (Issue #140)."""

import fcntl
import os
import threading
import time

import pytest

from cairnwork import runlock


@pytest.fixture
def ledger(tmp_path):
    return tmp_path / "ownership.sqlite"


@pytest.fixture
def recovering(ledger):
    """An exclusive holder, like recovery, on a separate open file description."""
    fd = os.open(runlock._lock_path(ledger), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    yield fd
    os.close(fd)


def test_shared_fd_gives_up_after_the_bound(ledger, recovering, monkeypatch):
    monkeypatch.setattr(runlock, "SHARED_WAIT_SECONDS", 0.2)
    start = time.monotonic()
    with pytest.raises(runlock.RunLockBusy, match="recovery held the run lock"):
        runlock.shared_fd(ledger)
    assert 0.2 <= time.monotonic() - start < 5


def test_shared_fd_closes_its_fd_when_it_gives_up(ledger, recovering, monkeypatch):
    monkeypatch.setattr(runlock, "SHARED_WAIT_SECONDS", 0.0)
    closed = []
    real_close = os.close
    monkeypatch.setattr(runlock.os, "close", lambda fd: (closed.append(fd), real_close(fd)))
    with pytest.raises(runlock.RunLockBusy):
        runlock.shared_fd(ledger)
    assert len(closed) == 1


def test_held_shared_gives_up_after_the_bound(ledger, recovering, monkeypatch):
    monkeypatch.setattr(runlock, "SHARED_WAIT_SECONDS", 0.2)
    entered = []
    with pytest.raises(runlock.RunLockBusy):
        with runlock.held(ledger, exclusive=False):
            entered.append(True)
    assert entered == []


def test_shared_wait_succeeds_when_recovery_ends_mid_wait(ledger, recovering, monkeypatch):
    monkeypatch.setattr(runlock, "SHARED_WAIT_SECONDS", 20.0)
    threading.Timer(0.3, fcntl.flock, (recovering, fcntl.LOCK_UN)).start()
    with runlock.held(ledger, exclusive=False):
        with pytest.raises(runlock.RunLockBusy):  # recovery is now kept out
            with runlock.held(ledger, exclusive=True):
                pass


def test_shared_holders_do_not_wait_for_each_other(ledger, monkeypatch):
    monkeypatch.setattr(runlock, "SHARED_WAIT_SECONDS", 0.0)
    fd = runlock.shared_fd(ledger)
    try:
        with runlock.held(ledger, exclusive=False):
            pass
    finally:
        os.close(fd)


def test_busy_keeps_its_code_and_default_detail():
    assert str(runlock.RunLockBusy()) == "RUN_LOCK_BUSY: a Cairnwork run is in progress"
