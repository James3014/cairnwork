"""Lock acquisition for the verifier account times out instead of hanging (PRESERVED #23a).

These do not need the dedicated account: only the lock file in a temp root is used.
"""

import fcntl
import multiprocessing
import os
import time

import pytest

from cairnwork.verifier_account import VerifierAccount, VerifierAccountError


def _hold(root: str, ready, release) -> None:
    fd = os.open(os.path.join(root, ".verifier.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    ready.set()
    release.wait(30)


@pytest.fixture
def holder(tmp_path):
    """A live process that holds the lock until told to stop."""
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=_hold, args=(str(tmp_path), ready, release))
    proc.start()
    assert ready.wait(20)
    yield release
    release.set()
    proc.join(20)


def test_second_request_times_out_while_holder_lives(tmp_path, holder):
    account = VerifierAccount(root=tmp_path)
    started = time.monotonic()
    with pytest.raises(VerifierAccountError, match="RESOURCE_LEASE_TIMEOUT"):
        with account.exclusive(timeout=0.3):
            pytest.fail("the lock is held by another process")
    assert 0.25 <= time.monotonic() - started < 10


def test_released_lock_is_taken_at_once(tmp_path):
    account = VerifierAccount(root=tmp_path)
    with account.exclusive(timeout=0.3):
        pass
    started = time.monotonic()
    with account.exclusive(timeout=30):
        pass
    assert time.monotonic() - started < 5


def test_waiter_gets_lock_when_holder_releases_within_timeout(tmp_path, holder):
    account = VerifierAccount(root=tmp_path)
    holder.set()
    with account.exclusive(timeout=20):
        pass


def test_lock_is_released_after_timeout_error(tmp_path, holder):
    account = VerifierAccount(root=tmp_path)
    with pytest.raises(VerifierAccountError):
        with account.exclusive(timeout=0.1):
            pass
    holder.set()
    with account.exclusive(timeout=20):
        pass
