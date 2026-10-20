"""Who is running: an OS file lock instead of PIDs.

Every run_task holds a shared flock on <run_root>/.cairnwork.lock while it works;
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

LOCK_NAME = ".cairnwork.lock"


class RunLockBusy(Exception):
    code = "RUN_LOCK_BUSY"

    def __init__(self) -> None:
        super().__init__(f"{self.code}: a Cairnwork run is in progress")


@contextmanager
def held(run_root: Path, *, exclusive: bool) -> Iterator[None]:
    """Hold the run lock; exclusive is non-blocking and raises RunLockBusy if taken."""
    Path(run_root).mkdir(parents=True, exist_ok=True)
    fd = os.open(Path(run_root) / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
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
