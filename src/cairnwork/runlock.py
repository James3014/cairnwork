"""Who is running: an OS file lock instead of PIDs.

Every run_task holds a shared flock on a lock file next to the ownership ledger
("<ledger>.lock") while it works, so the lock covers every task that ledger knows,
whatever run directory a caller passes;
recovery needs the exclusive lock, so it can only run when no Cairnwork process
is mid-task. The kernel drops the lock when the process dies, however it dies,
so there is no stale lock to clean up and no PID that could be reused (Nexus-new
#2646 zombie PIDs, #2717 unreclaimable lease receipts). Child processes do not
inherit the lock (subprocess closes file descriptors by default), except where
``shared_fd`` hands it on: intake binds a task and passes the lock to the runner
it starts, so recovery cannot settle the task between the two.

Recovery takes the exclusive lock without waiting; a run waits for its shared lock at most
SHARED_WAIT_SECONDS (read at call time), then raises RunLockBusy instead of queueing forever
behind a recovery that hangs (defence in depth, Issue #140). Nothing is bound or started before
the shared lock is held, so giving up leaves no state behind.
"""

from __future__ import annotations

import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# Longer than any healthy recovery: it waits up to the verifier account's 900 s lock floor,
# then a bounded idle probe and the settle loop.
SHARED_WAIT_SECONDS = 3600.0
_POLL_SECONDS = 0.05


class RunLockBusy(Exception):
    code = "RUN_LOCK_BUSY"

    def __init__(self, detail: str = "a Cairnwork run is in progress") -> None:
        super().__init__(f"{self.code}: {detail}")


def shared_fd(ledger_path: Path) -> int:
    """Open and share-lock the run lock; the caller owns (and must close) the fd."""
    fd = os.open(_lock_path(ledger_path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        _share(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _share(fd: int) -> None:
    """Take the shared lock, polling until SHARED_WAIT_SECONDS has passed (RunLockBusy)."""
    wait = SHARED_WAIT_SECONDS
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RunLockBusy(f"recovery held the run lock for over {wait:g}s") from None
            time.sleep(min(_POLL_SECONDS, remaining))


def _lock_path(ledger_path: Path) -> Path:
    return Path(ledger_path).with_name(Path(ledger_path).name + ".lock")


@contextmanager
def held(ledger_path: Path, *, exclusive: bool) -> Iterator[None]:
    """Hold the run lock of a ledger; exclusive is non-blocking (RunLockBusy if taken), shared
    waits at most SHARED_WAIT_SECONDS (RunLockBusy after that)."""
    fd = os.open(_lock_path(ledger_path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if exclusive:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RunLockBusy() from exc
        else:
            _share(fd)
        yield
    finally:
        os.close(fd)
