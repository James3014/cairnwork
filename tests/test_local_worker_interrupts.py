"""The cleanup and the corpus line survive a second SIGTERM and a SystemExit (Claude review of
#239, NB8). The first SIGTERM/SIGINT interrupts the loop; any later one is only counted; a
SystemExit is recorded, the cleanup still runs and the line is written before it is re-raised."""

from __future__ import annotations

import os
import signal

import local_worker_fakes
import pytest
from local_worker_fakes import META, TASK, World, cli_harness, lines, make_loop, reply

from experimental.local_worker import episode
from experimental.local_worker.agent import initial_messages
from experimental.local_worker.episode_schema import validate_record

READ = reply("read_file", path="src/app.py")


def test_only_the_first_signal_interrupts(tmp_path):
    loop, _, _, _ = make_loop(tmp_path, [])
    with pytest.raises(KeyboardInterrupt):
        loop.interrupt(signal.SIGTERM, None)
    loop.interrupt(signal.SIGTERM, None)
    loop.interrupt(signal.SIGINT, None)
    assert loop.late_signals == 2


class Raising(World):
    """Raises ``exc`` from the wrapper call ``on`` (once)."""

    def __init__(self, on: str, exc: BaseException) -> None:
        super().__init__()
        self.on, self.exc = on, exc

    def handle(self, action, args, stdin):
        if action == self.on and self.exc is not None:
            exc, self.exc = self.exc, None
            raise exc
        return super().handle(action, args, stdin)


def test_a_system_exit_during_cleanup_leaves_a_valid_failed_cleanup(tmp_path):
    loop, world, _, _ = make_loop(tmp_path, [READ], world=Raising("sweep", SystemExit(3)))
    out = loop.run(initial_messages(TASK))  # does not raise
    assert out.cleanup == "failed" and out.exit_reason == "COMPLETED"
    assert isinstance(loop.deferred, SystemExit) and loop.deferred.code == 3
    validate_record(episode.build_record(META, out))
    assert [c[0] for c in world.calls][-1] == "sweep"  # numstat never started


def test_a_system_exit_in_the_loop_is_recorded_and_cleaned_up(tmp_path):
    loop, world, _, _ = make_loop(tmp_path, [READ], world=Raising("read", SystemExit(4)))
    out = loop.run(initial_messages(TASK))
    assert out.exit_reason == "ERROR_ABORT" and out.abort_cause == "interrupted"
    assert out.cleanup == "ok" and isinstance(loop.deferred, SystemExit)
    # the read had started: after sweep and numstat its tree is observed (_observe_last)
    assert [c[0] for c in world.calls][-3:] == ["sweep", "numstat", "status"]
    validate_record(episode.build_record(META, out))


class Signalling(World):
    """Sends this process SIGTERM during a read (the loop) and again during the sweep."""

    def handle(self, action, args, stdin):
        if action in ("read", "sweep"):
            os.kill(os.getpid(), signal.SIGTERM)
        return super().handle(action, args, stdin)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    return cli_harness(tmp_path, monkeypatch)


def test_a_second_sigterm_during_cleanup_does_not_stop_it(harness, monkeypatch):
    run, out = harness
    monkeypatch.setattr(local_worker_fakes, "World", Signalling)
    before = signal.getsignal(signal.SIGTERM)
    assert run([READ], "run") == 0
    [rec] = lines(out)
    assert rec["termination"]["abort_cause"] == "interrupted"
    assert rec["termination"]["cleanup"] == "ok" and rec["verification"]["patch_lines_added"] == 0
    assert signal.getsignal(signal.SIGTERM) is before  # the handler is put back afterwards


def test_a_system_exit_is_raised_only_after_the_line_is_written(harness, monkeypatch):
    run, out = harness
    monkeypatch.setattr(local_worker_fakes, "World", lambda: Raising("read", SystemExit(5)))
    with pytest.raises(SystemExit) as exc:
        run([READ], "run")
    assert exc.value.code == 5
    [rec] = lines(out)
    assert rec["termination"]["abort_cause"] == "interrupted"
    assert rec["termination"]["cleanup"] == "ok"
