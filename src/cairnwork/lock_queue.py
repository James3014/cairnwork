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


def _tickets(dfd: int) -> Iterator[tuple[int, str]]:
    for name in os.listdir(dfd):
        if _TICKET_NAME.fullmatch(name):
            yield int(name.split(".", 1)[0]), name


def _open_queue(queue_dir: Path) -> int:
    try:
        os.mkdir(queue_dir, 0o700)
    except FileExistsError:
        pass  # validate the opened directory, whether newly created or already present
    dfd = os.open(queue_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(dfd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise OSError(f"unsafe verifier queue directory: {queue_dir}") from None
    except OSError:
        os.close(dfd)
        raise
    return dfd


def _unlink(dfd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=dfd)
    except FileNotFoundError:
        pass  # another waiter may already have reclaimed this stale entry


def _reclaim_temporary(dfd: int) -> None:
    """Called with .alloc held: no live allocator can have an unpublished temp."""
    for name in os.listdir(dfd):
        if not name.startswith(".tmp-"):
            continue
        try:
            info = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                _unlink(dfd, name)
                continue
            fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=dfd)
        except FileNotFoundError:
            continue
        try:
            if stat.S_ISREG(os.fstat(fd).st_mode):
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
            _unlink(dfd, name)
        finally:
            os.close(fd)


def _is_my_turn(dfd: int, number: int) -> bool:
    for older, name in _tickets(dfd):
        if older >= number:
            continue
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
        except FileNotFoundError:
            continue
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                _unlink(dfd, name)
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            _unlink(dfd, name)
        finally:
            os.close(fd)
    return True


@contextmanager
def ticket(queue_dir: Path, deadline: float) -> Iterator[Callable[[], bool]]:
    """Publish before a monotonic deadline (try at least once); clean up on every exit."""
    dfd = _open_queue(queue_dir)
    fd: int | None = None
    name: str | None = None
    try:
        alloc = os.open(
            ".alloc", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=dfd
        )
        try:
            if not stat.S_ISREG(os.fstat(alloc).st_mode):
                raise OSError("unsafe verifier queue allocator: not a regular file")
            while True:
                try:
                    fcntl.flock(alloc, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TicketTimeout from None
                    time.sleep(min(0.01, remaining))
            _reclaim_temporary(dfd)
            number = 1 + max((n for n, _ in _tickets(dfd)), default=0)
            temporary = f".tmp-{uuid.uuid4().hex}"
            fd = os.open(
                temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd
            )
            name = temporary
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            final = f"{number:020d}.{os.getpid()}"
            os.rename(name, final, src_dir_fd=dfd, dst_dir_fd=dfd)
            name = final
        finally:
            os.close(alloc)
        yield lambda: _is_my_turn(dfd, number)
    finally:
        try:
            if name is not None:
                _unlink(dfd, name)
        finally:
            try:
                if fd is not None:
                    os.close(fd)
            finally:
                os.close(dfd)
