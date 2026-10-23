"""FIFO tickets for a polled lock; each live waiter's ticket holds a flock."""

from __future__ import annotations

import fcntl
import os
import re
import stat
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

_TICKET_NAME = re.compile(r"[0-9]{20}\.[0-9]+")


class TicketTimeout(Exception):
    """The lock wait deadline passed before a ticket could be allocated."""


def _tickets(queue_dir: Path) -> Iterator[tuple[int, Path]]:
    for path in queue_dir.iterdir():
        if _TICKET_NAME.fullmatch(path.name):
            yield int(path.name.split(".", 1)[0]), path


def _ensure_queue(queue_dir: Path) -> None:
    try:
        os.mkdir(queue_dir, 0o700)
    except FileExistsError:
        info = os.lstat(queue_dir)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise OSError(f"unsafe verifier queue directory: {queue_dir}") from None


def _is_my_turn(queue_dir: Path, number: int) -> bool:
    for older, path in _tickets(queue_dir):
        if older >= number:
            continue
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            path.unlink(missing_ok=True)
        finally:
            os.close(fd)
    return True


@contextmanager
def ticket(queue_dir: Path, deadline: float) -> Iterator[Callable[[], bool]]:
    """Publish before a monotonic deadline (try at least once); clean up on every exit."""
    _ensure_queue(queue_dir)
    alloc = os.open(queue_dir / ".alloc", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    fd: int | None = None
    path: Path | None = None
    try:
        try:
            while True:
                try:
                    fcntl.flock(alloc, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TicketTimeout from None
                    time.sleep(min(0.01, remaining))
            number = 1 + max((n for n, _ in _tickets(queue_dir)), default=0)
            temporary = queue_dir / f".tmp-{uuid.uuid4().hex}"
            fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            path = temporary
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            final = queue_dir / f"{number:020d}.{os.getpid()}"
            os.rename(path, final)
            path = final
        finally:
            os.close(alloc)
        yield lambda: _is_my_turn(queue_dir, number)
    finally:
        try:
            if path is not None:
                path.unlink(missing_ok=True)
        finally:
            if fd is not None:
                os.close(fd)
