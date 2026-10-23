"""Cross-process FIFO fairness without a dedicated verifier account."""

import fcntl
import multiprocessing
import os
import re
import time
from pathlib import Path

import pytest

from cairnwork.lock_queue import ticket
from cairnwork.verifier_account import VerifierAccount, VerifierAccountError


def _tickets(root: Path) -> list[Path]:
    queue = root / ".verifier.queue"
    if not queue.exists():
        return []
    return sorted(p for p in queue.iterdir() if re.fullmatch(r"[0-9]{20}\.[0-9]+", p.name))


def _wait_for_ticket(root: Path, pid: int) -> Path:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        for path in _tickets(root):
            if path.name.endswith(f".{pid}"):
                return path
        time.sleep(0.01)
    pytest.fail(f"process {pid} did not publish its ticket")


def _hold_main(root, ready, release):
    fd = os.open(root / ".verifier.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        ready.set()
        release.wait(10)
    finally:
        os.close(fd)


def _hold_ticket(root, ready, release):
    with ticket(root / ".verifier.queue", time.monotonic() + 10):
        ready.set()
        release.wait(10)


def _hold_allocator(root, ready, release):
    queue = root / ".verifier.queue"
    queue.mkdir(mode=0o700)
    fd = os.open(queue / ".alloc", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        ready.set()
        release.wait(10)
    finally:
        os.close(fd)


def _waiter(root, label, acquired):
    with VerifierAccount(root=root).exclusive(timeout=10):
        acquired.put(label)
        time.sleep(0.3)


def _acquire_once(root):
    with VerifierAccount(root=root).exclusive(timeout=0.5):
        pass


def _reject_unsafe_queue(root):
    with pytest.raises(VerifierAccountError, match="queue"):
        with VerifierAccount(root=root).exclusive(timeout=0.5):
            pytest.fail("an unsafe queue must not be acquired")


@pytest.fixture
def processes():
    ctx = multiprocessing.get_context("spawn")
    children = []

    def start(target, *args):
        child = ctx.Process(target=target, args=args)
        child.start()
        children.append(child)
        return child

    try:
        yield ctx, start
    finally:
        for child in children:
            if child.is_alive():
                child.kill()
            child.join(10)


def test_waiters_acquire_in_ticket_order(tmp_path, processes):
    ctx, start = processes
    ready, release = ctx.Event(), ctx.Event()
    holder = start(_hold_main, tmp_path, ready, release)
    assert ready.wait(10)
    acquired = ctx.Queue()
    try:
        first = start(_waiter, tmp_path, "A", acquired)
        first_ticket = _wait_for_ticket(tmp_path, first.pid)
        second = start(_waiter, tmp_path, "B", acquired)
        second_ticket = _wait_for_ticket(tmp_path, second.pid)
        assert first_ticket.name < second_ticket.name
        release.set()
        assert acquired.get(timeout=10) == "A"
        assert acquired.get(timeout=10) == "B"
        for child in (holder, first, second):
            child.join(10)
            assert child.exitcode == 0
        assert _tickets(tmp_path) == []
    finally:
        acquired.close()
        acquired.join_thread()


def test_live_older_ticket_prevents_barging_even_when_main_lock_is_free(tmp_path, processes):
    ctx, start = processes
    ready, release = ctx.Event(), ctx.Event()
    older = start(_hold_ticket, tmp_path, ready, release)
    assert ready.wait(10)
    older_ticket = _wait_for_ticket(tmp_path, older.pid)
    account = VerifierAccount(root=tmp_path)
    with pytest.raises(VerifierAccountError, match="RESOURCE_LEASE_TIMEOUT"):
        with account.exclusive(timeout=0):
            pytest.fail("a newcomer must not pass a live older ticket")
    assert _tickets(tmp_path) == [older_ticket]  # newcomer's timed-out ticket was removed
    release.set()
    older.join(10)
    assert older.exitcode == 0
    with account.exclusive(timeout=0):
        assert _tickets(tmp_path) == []


def test_unlocked_stale_ticket_is_removed_and_other_names_are_ignored(tmp_path):
    queue = tmp_path / ".verifier.queue"
    queue.mkdir(mode=0o700)
    stale = queue / "00000000000000000001.12345"
    stale.touch(mode=0o600)
    ignored = [queue / "notes", queue / "1.12345"]
    for path in ignored:
        path.touch()
    started = time.monotonic()
    with VerifierAccount(root=tmp_path).exclusive(timeout=0):
        assert not stale.exists()
        assert _tickets(tmp_path) == []
    assert time.monotonic() - started < 5
    assert all(path.exists() for path in ignored)


def test_killed_waiters_ticket_is_recovered(tmp_path, processes):
    ctx, start = processes
    ready, release = ctx.Event(), ctx.Event()
    older = start(_hold_ticket, tmp_path, ready, release)
    assert ready.wait(10)
    stale = _wait_for_ticket(tmp_path, older.pid)
    older.kill()
    older.join(10)
    assert older.exitcode != 0
    assert stale.exists()
    with VerifierAccount(root=tmp_path).exclusive(timeout=0):
        assert not stale.exists()
        assert _tickets(tmp_path) == []


def test_ticket_is_removed_before_yield_and_after_normal_exit(tmp_path):
    with VerifierAccount(root=tmp_path).exclusive(timeout=0):
        assert _tickets(tmp_path) == []
    assert _tickets(tmp_path) == []


def test_ticket_is_removed_after_exception_in_body(tmp_path):
    with pytest.raises(OSError, match="body failed"):
        with VerifierAccount(root=tmp_path).exclusive(timeout=0):
            raise OSError("body failed")
    assert _tickets(tmp_path) == []
    with VerifierAccount(root=tmp_path).exclusive(timeout=0):
        pass  # the main lock was also released


def test_ticket_is_removed_after_timeout(tmp_path):
    fd = os.open(tmp_path / ".verifier.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with pytest.raises(VerifierAccountError, match="RESOURCE_LEASE_TIMEOUT"):
            with VerifierAccount(root=tmp_path).exclusive(timeout=0.08):
                pytest.fail("the main lock is still held")
        assert _tickets(tmp_path) == []
    finally:
        os.close(fd)
    with VerifierAccount(root=tmp_path).exclusive(timeout=0):
        pass


def test_allocator_contention_times_out_without_publishing_ticket(tmp_path, processes):
    ctx, start = processes
    ready, release = ctx.Event(), ctx.Event()
    holder = start(_hold_allocator, tmp_path, ready, release)
    assert ready.wait(10)
    account = VerifierAccount(root=tmp_path)
    started = time.monotonic()
    with pytest.raises(VerifierAccountError, match="RESOURCE_LEASE_TIMEOUT") as error:
        with account.exclusive(timeout=0.3):
            pytest.fail("the allocator is still held")
    assert 0.25 <= time.monotonic() - started < 5
    assert str(error.value) == (
        f"VERIFIER_ACCOUNT_ERROR: RESOURCE_LEASE_TIMEOUT: account {account.user} busy for 0.3s"
    )
    assert sorted(p.name for p in (tmp_path / ".verifier.queue").iterdir()) == [".alloc"]
    release.set()
    holder.join(10)
    assert holder.exitcode == 0
    with account.exclusive(timeout=0):
        pass


@pytest.mark.parametrize("mode", [0o777, 0o770, 0o707])
def test_queue_writable_by_group_or_other_is_rejected(tmp_path, mode):
    queue = tmp_path / ".verifier.queue"
    queue.mkdir(mode=0o700)
    queue.chmod(mode)
    with pytest.raises(VerifierAccountError, match="queue"):
        with VerifierAccount(root=tmp_path).exclusive(timeout=0):
            pytest.fail("a writable queue directory is unsafe")
    assert list(queue.iterdir()) == []


def test_symlinked_queue_is_rejected_without_touching_target(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / ".verifier.queue").symlink_to(target, target_is_directory=True)
    with pytest.raises(VerifierAccountError, match="queue"):
        with VerifierAccount(root=tmp_path).exclusive(timeout=0):
            pytest.fail("a symlink is not a safe queue directory")
    assert list(target.iterdir()) == []


def test_symlinked_allocator_is_rejected_without_touching_target(tmp_path):
    queue = tmp_path / ".verifier.queue"
    queue.mkdir(mode=0o700)
    target = tmp_path / "elsewhere"
    (queue / ".alloc").symlink_to(target)
    with pytest.raises(VerifierAccountError, match="queue"):
        with VerifierAccount(root=tmp_path).exclusive(timeout=0):
            pytest.fail("the allocator must not follow symlinks")
    assert not target.exists()
    assert _tickets(tmp_path) == []


def test_fifo_older_ticket_is_removed_without_blocking(tmp_path, processes):
    _, start = processes
    queue = tmp_path / ".verifier.queue"
    queue.mkdir(mode=0o700)
    fifo = queue / "00000000000000000001.12345"
    os.mkfifo(fifo, 0o600)
    child = start(_acquire_once, tmp_path)
    child.join(10)
    assert not child.is_alive(), "opening an older FIFO ticket blocked acquisition"
    assert child.exitcode == 0
    assert not fifo.exists()
    assert _tickets(tmp_path) == []


@pytest.mark.parametrize("kind", ["fifo", "directory"])
def test_nonregular_allocator_is_rejected_without_blocking(tmp_path, processes, kind):
    _, start = processes
    queue = tmp_path / ".verifier.queue"
    queue.mkdir(mode=0o700)
    alloc = queue / ".alloc"
    if kind == "fifo":
        os.mkfifo(alloc, 0o600)
    else:
        alloc.mkdir()
    child = start(_reject_unsafe_queue, tmp_path)
    child.join(10)
    assert not child.is_alive(), "opening a nonregular allocator blocked acquisition"
    assert child.exitcode == 0
    assert list(queue.iterdir()) == [alloc]


def test_unpublished_temp_debris_is_reclaimed(tmp_path):
    queue = tmp_path / ".verifier.queue"
    queue.mkdir(mode=0o700)
    (queue / ".tmp-xxx").touch(mode=0o600)
    os.mkfifo(queue / ".tmp-fifo", 0o600)
    target = tmp_path / "elsewhere"
    target.write_text("untouched")
    (queue / ".tmp-symlink").symlink_to(target)
    with VerifierAccount(root=tmp_path).exclusive(timeout=0.5):
        assert sorted(p.name for p in queue.iterdir()) == [".alloc"]
    assert target.read_text() == "untouched"


def test_locked_temp_is_retained_until_its_lock_is_released(tmp_path):
    queue = tmp_path / ".verifier.queue"
    queue.mkdir(mode=0o700)
    temporary = queue / ".tmp-locked"
    fd = os.open(temporary, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with VerifierAccount(root=tmp_path).exclusive(timeout=0.5):
            assert temporary.exists()
    finally:
        os.close(fd)
    with VerifierAccount(root=tmp_path).exclusive(timeout=0.5):
        assert not temporary.exists()


@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_published_ticket_uses_original_directory_after_path_substitution(tmp_path, replacement):
    queue = tmp_path / ".verifier.queue"
    queue.mkdir(mode=0o700)
    stale_name = "00000000000000000001.12345"
    (queue / stale_name).touch(mode=0o600)
    moved = tmp_path / "original-queue"
    with ticket(queue, time.monotonic() + 0.5) as is_my_turn:
        own_name = next(p.name for p in _tickets(tmp_path) if p.name != stale_name)
        queue.rename(moved)
        if replacement == "symlink":
            target = tmp_path / "elsewhere"
            target.mkdir(mode=0o700)
            queue.symlink_to(target, target_is_directory=True)
        else:
            queue.mkdir(mode=0o700)
        for name in (stale_name, own_name):
            (queue / name).write_text("untouched")
        assert is_my_turn()
        assert not (moved / stale_name).exists()
        assert (moved / own_name).exists()
    assert sorted(p.name for p in moved.iterdir()) == [".alloc"]
    assert sorted(p.name for p in queue.iterdir()) == sorted([stale_name, own_name])
    assert all(p.read_text() == "untouched" for p in queue.iterdir())
