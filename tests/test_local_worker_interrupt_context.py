"""A SIGTERM/SIGINT landing while another exception unwinds (reviews of PR #248, W2 and round 3).

The interrupt replaces the exception in flight, which stays its ``__context__``. Only a SystemExit
context is deferred to the caller (re-raised after the line); an Exception or a GeneratorExit
context is not, and the CLI exits 0 with the line written.
"""

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


@pytest.fixture
def harness(tmp_path, monkeypatch):
    return cli_harness(tmp_path, monkeypatch)


@pytest.fixture
def on_sigterm():
    """Installs a loop's ``interrupt`` for SIGTERM (as cli.py does) and restores the old one."""
    before = signal.getsignal(signal.SIGTERM)
    yield lambda loop: signal.signal(signal.SIGTERM, loop.interrupt)
    signal.signal(signal.SIGTERM, before)


def _run(loop):
    try:
        return loop.run(initial_messages(TASK))
    except KeyboardInterrupt:  # pytest would stop the whole session on it
        pytest.fail("a signal escaped run()")


def test_w2_a_signal_while_a_system_exit_unwinds_keeps_the_exit(tmp_path, on_sigterm, monkeypatch):
    """Review of PR #248, W2: the KeyboardInterrupt replaces the SystemExit in flight; the exit is
    still deferred to the caller (it was lost and the CLI exited 0)."""
    loop, _, _, _ = make_loop(tmp_path, [READ])
    on_sigterm(loop)
    exit_seven = SystemExit(7)

    def step(log, messages, deadline):
        try:
            raise exit_seven
        finally:
            os.kill(os.getpid(), signal.SIGTERM)  # arrives while SystemExit unwinds

    monkeypatch.setattr(loop, "_step", step)
    out = _run(loop)
    assert out.abort_cause == "interrupted" and loop.deferred is exit_seven
    assert out.cleanup == "ok" and loop.late_signals == 0
    validate_record(episode.build_record(META, out))


def test_w2_the_cli_exits_with_the_replaced_system_exit(harness, monkeypatch):
    run, out = harness

    class ExitThenSignal(World):
        def handle(self, action, args, stdin):
            if action == "read":
                try:
                    raise SystemExit(9)
                finally:
                    os.kill(os.getpid(), signal.SIGTERM)
            return super().handle(action, args, stdin)

    monkeypatch.setattr(local_worker_fakes, "World", ExitThenSignal)
    with pytest.raises(SystemExit) as exc:
        run([READ], "run")
    assert exc.value.code == 9
    [rec] = lines(out)
    assert rec["termination"]["abort_cause"] == "interrupted"


def _signal_while_unwinding(monkeypatch, loop, unwind):
    """_step's turn: ``unwind`` runs; a SIGTERM lands while its exception is being handled."""

    def step(log, messages, deadline):
        unwind()

    monkeypatch.setattr(loop, "_step", step)


def test_w4_a_signal_while_a_generator_closes_defers_nothing(tmp_path, on_sigterm, monkeypatch):
    """Review of PR #248 round 3, W4: the interrupt's context is a GeneratorExit, not an exit."""

    def gen():
        try:
            yield
        finally:  # runs during close(), with GeneratorExit being raised
            os.kill(os.getpid(), signal.SIGTERM)

    def close_a_generator():
        g = gen()
        next(g)
        g.close()

    loop, _, _, _ = make_loop(tmp_path, [READ])
    on_sigterm(loop)
    _signal_while_unwinding(monkeypatch, loop, close_a_generator)
    out = _run(loop)
    assert out.abort_cause == "interrupted" and loop.deferred is None
    validate_record(episode.build_record(META, out))


def _raise_then_signal():
    try:
        raise RuntimeError("being handled")
    finally:
        os.kill(os.getpid(), signal.SIGTERM)  # the interrupt's context is the RuntimeError


def test_a_signal_while_an_exception_unwinds_defers_nothing(tmp_path, on_sigterm, monkeypatch):
    """Review of PR #248 round 3, item 3: an Exception context is never deferred (mutant P5)."""
    loop, _, _, _ = make_loop(tmp_path, [READ])
    on_sigterm(loop)
    _signal_while_unwinding(monkeypatch, loop, _raise_then_signal)
    out = _run(loop)
    assert out.abort_cause == "interrupted" and loop.deferred is None
    validate_record(episode.build_record(META, out))


def test_the_cli_exits_0_after_a_signal_while_an_exception_unwinds(harness, monkeypatch):
    run, out = harness

    class RaiseThenSignal(World):
        def handle(self, action, args, stdin):
            if action == "read":
                _raise_then_signal()
            return super().handle(action, args, stdin)

    monkeypatch.setattr(local_worker_fakes, "World", RaiseThenSignal)
    assert run([READ], "run") == 0  # nothing re-raised
    [rec] = lines(out)
    assert rec["termination"]["abort_cause"] == "interrupted"
