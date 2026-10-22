"""Lock acquisition for the verifier account times out instead of hanging (PRESERVED #23a).

These do not need the dedicated account: only the lock file in a temp root is used.
"""

import fcntl
import multiprocessing
import os
import threading
import time

import pytest

from cairnwork import verifier_account, verifiers
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


def test_waiter_gets_lock_when_holder_releases_mid_wait(tmp_path, holder):
    account = VerifierAccount(root=tmp_path)
    timer = threading.Timer(0.5, holder.set)
    timer.start()
    started = time.monotonic()
    try:
        with account.exclusive(timeout=20):
            waited = time.monotonic() - started
    finally:
        timer.join()
    assert 0.4 <= waited < 15  # it really waited for the holder, then took the lock


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0, -0.001])
def test_unusable_timeout_is_rejected(tmp_path, bad):
    with pytest.raises(ValueError, match="timeout"):
        with VerifierAccount(root=tmp_path).exclusive(timeout=bad):
            pytest.fail("must not enter the section")


def test_zero_timeout_tries_once(tmp_path, holder):
    account = VerifierAccount(root=tmp_path)
    with pytest.raises(VerifierAccountError, match="RESOURCE_LEASE_TIMEOUT"):
        with account.exclusive(timeout=0):
            pass
    holder.set()
    time.sleep(0.5)
    with account.exclusive(timeout=0):
        pass


def test_last_sleep_is_clamped_to_the_remaining_time(tmp_path, holder, monkeypatch):
    monkeypatch.setattr(verifier_account, "_LOCK_POLL_SECONDS", 2.0)
    account = VerifierAccount(root=tmp_path)
    started = time.monotonic()
    with pytest.raises(VerifierAccountError):
        with account.exclusive(timeout=0.2):
            pass
    assert time.monotonic() - started < 1.0  # an unclamped sleep would overshoot to 2 s


def test_default_timeout_is_read_at_call_time(tmp_path, holder, monkeypatch):
    monkeypatch.setattr(verifier_account, "LOCK_TIMEOUT_SECONDS", 0.2)
    with pytest.raises(VerifierAccountError, match="RESOURCE_LEASE_TIMEOUT"):
        with VerifierAccount(root=tmp_path).exclusive():
            pass


def test_lock_timeout_ends_the_task_failed(repo, tmp_path, holder, monkeypatch):
    """Pipeline level: a verifier account that stays busy fails the task, never hangs it."""
    from conftest import commit_change, git

    from cairnwork.journal import Journal
    from cairnwork.notary import Notary
    from cairnwork.ownership import OwnershipLedger
    from cairnwork.pipeline import TaskSpec, run_task

    monkeypatch.setattr(verifiers, "lock_wait_seconds", lambda *_: 0.3)  # derived wait is minutes
    commit_change(
        repo,
        {
            "src/calc.py": "def add(a, b):\n    return a + b + 1\n",
            "tests/test_calc.py": "def test_add():\n    assert True\n",
        },
    )
    spec = TaskSpec(
        "k1",
        "card-k1",
        "fix add",
        "add() is off by one",
        repo,
        git(repo, "rev-parse", "HEAD"),
        ("src/calc.py",),
        (("pytest", ("python3", "-m", "pytest", "-q", "tests/test_calc.py")),),
    )

    class Worker:
        def run(self, workdir, task):
            (workdir / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")

    ledger = OwnershipLedger(tmp_path / "own.sqlite")
    result = run_task(
        spec,
        ledger=ledger,
        notary=Notary(tmp_path / "notary.sqlite"),
        journal=Journal(tmp_path / "journal.sqlite"),
        worker=Worker(),
        run_root=tmp_path / "runs",
        verifier_account=VerifierAccount(root=tmp_path),
        verifier_python="/usr/bin/python3",
    )
    assert result.status == "FAILED" and not result.verified
    assert "RESOURCE_LEASE_TIMEOUT" in result.reason
    assert ledger.get(task_id=result.task_id).status == "FAILED"


def test_lock_is_released_after_timeout_error(tmp_path, holder):
    account = VerifierAccount(root=tmp_path)
    with pytest.raises(VerifierAccountError):
        with account.exclusive(timeout=0.1):
            pass
    holder.set()
    with account.exclusive(timeout=20):
        pass


@pytest.mark.parametrize(
    ("count", "per_spec", "expected"),
    [(1, 600, 900.0), (2, 600, 1500.0), (3, 600, 2100.0), (0, 600, 900.0), (5, 10, 900.0)],
)
def test_lock_wait_grows_with_spec_count_and_never_drops_below_the_floor(count, per_spec, expected):
    assert verifier_account.lock_wait_seconds(count, per_spec) == expected


def test_lock_wait_floor_is_read_at_call_time(monkeypatch):
    monkeypatch.setattr(verifier_account, "LOCK_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(verifier_account, "_LOCK_MARGIN_SECONDS", 1.0)
    assert verifier_account.lock_wait_seconds(0, 600) == 5.0
    assert verifier_account.lock_wait_seconds(1, 600) == 601.0


def test_run_verifiers_waits_for_the_lock_as_long_as_its_specs_need(tmp_path):
    seen = []

    class Spy(VerifierAccount):
        def exclusive(self, timeout=None):
            seen.append(timeout)
            raise VerifierAccountError("stop here")

    specs = tuple(verifiers.VerifierSpec(f"v{i}", ("tests/t.py",)) for i in range(3))
    with pytest.raises(VerifierAccountError, match="stop here"):
        verifiers.run_verifiers(
            tmp_path, "0" * 40, specs, tmp_path, "r", account=Spy(root=tmp_path), timeout=100
        )
    assert seen == [max(verifier_account.LOCK_TIMEOUT_SECONDS, 3 * 100 + 300.0)]
