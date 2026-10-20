"""Who is running: an OS file lock instead of PIDs.

Every run_task holds a shared flock on a lock file next to the ownership ledger
("<ledger>.lock") while it works, so the lock covers every task that ledger knows,
whatever run directory a caller passes;
recovery needs the exclusive lock, so it can only run when no Cairnwork process
is mid-task. The kernel drops the lock when the process dies, however it dies,
so there is no stale lock to clean up and no PID that could be reused (Nexus-new
#2646 zombie PIDs, #2717 unreclaimable lease receipts). Child processes do not
inherit the lock (subprocess closes file descriptors by default).
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class RunLockBusy(Exception):
    code = "RUN_LOCK_BUSY"

    def __init__(self) -> None:
        super().__init__(f"{self.code}: a Cairnwork run is in progress")


@contextmanager
def held(ledger_path: Path, *, exclusive: bool) -> Iterator[None]:
    """Hold the run lock of a ledger; exclusive is non-blocking (RunLockBusy if taken)."""
    lock = Path(ledger_path).with_name(Path(ledger_path).name + ".lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if exclusive:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RunLockBusy() from exc
        else:
            fcntl.flock(fd, fcntl.LOCK_SH)
        yield
    finally:
        os.close(fd)
