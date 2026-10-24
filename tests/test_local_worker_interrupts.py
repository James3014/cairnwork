"""The cleanup and the corpus line survive a second SIGTERM and a SystemExit (Claude review of
#239, NB8). The first SIGTERM/SIGINT interrupts the loop; any later one is only counted; a
SystemExit is recorded, the cleanup still runs and the line is written before it is re-raised."""

from __future__ import annotations

import json
import os
import signal

import local_worker_fakes
import pytest
from local_worker_fakes import META, TASK, World, cli_harness, lines, make_loop, reply

from experimental.local_worker import cli, corpus_io, episode
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


def _transcript(out, rec):
    return json.loads((out / "private" / rec["episode_id"] / "transcript.json").read_text())


def test_late_signals_reach_the_private_transcript_only(harness, monkeypatch):
    run, out = harness
    monkeypatch.setattr(local_worker_fakes, "World", Signalling)
    assert run([READ], "run") == 0
    [rec] = lines(out)
    assert _transcript(out, rec)["late_signals"] == 1  # the second SIGTERM, during the sweep
    assert "late_signals" not in json.dumps(rec)


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


def _sigterm_then(real):
    def call(*args, **kwargs):
        os.kill(os.getpid(), signal.SIGTERM)
        return real(*args, **kwargs)

    return call


def test_a_signal_while_a_stop_is_recorded_is_only_counted(tmp_path, on_sigterm, monkeypatch):
    loop, _, _, _ = make_loop(tmp_path, [READ], world=Raising("read", RuntimeError("boom")))
    on_sigterm(loop)
    monkeypatch.setattr(loop, "_abort", _sigterm_then(loop._abort))  # while it is recorded
    out = _run(loop)
    assert out.exit_reason == "ERROR_ABORT" and out.abort_cause == "internal_error"
    assert out.cleanup == "ok" and loop.late_signals == 1  # the recorded stop stands
    validate_record(episode.build_record(META, out))


@pytest.mark.parametrize(
    ("exc", "cause"), [(RuntimeError("boom"), "internal_error"), (SystemExit(7), "interrupted")]
)
def test_a_signal_as_the_stop_is_being_recorded_keeps_that_stop(
    tmp_path, on_sigterm, monkeypatch, exc, cause
):
    loop, _, _, _ = make_loop(tmp_path, [READ], world=Raising("read", exc))
    on_sigterm(loop)
    real, calls = loop._stopped, []

    def stopped(out, caught):  # the signal lands before _stopped's first line runs
        calls.append(caught)
        if len(calls) == 1:
            os.kill(os.getpid(), signal.SIGTERM)
        return real(out, caught)

    monkeypatch.setattr(loop, "_stopped", stopped)
    out = _run(loop)
    assert calls == [exc, exc] and out.abort_cause == cause and loop.late_signals == 1
    assert loop.deferred is (exc if isinstance(exc, SystemExit) else None)
    assert out.cleanup == "ok"
    validate_record(episode.build_record(META, out))


def test_a_signal_after_the_model_finished_is_only_counted(tmp_path, on_sigterm, monkeypatch):
    loop, _, _, _ = make_loop(tmp_path, [READ])
    on_sigterm(loop)
    real = loop._loop

    def loop_then_signal(*args):
        real(*args)
        os.kill(os.getpid(), signal.SIGTERM)  # the stop is set; the handler runs on return

    monkeypatch.setattr(loop, "_loop", loop_then_signal)
    out = _run(loop)
    assert out.exit_reason == "COMPLETED" and not out.interrupted and out.cleanup == "ok"
    assert loop.late_signals == 1
    validate_record(episode.build_record(META, out))


def test_a_signal_before_the_loop_starts_escapes_and_no_line_is_written(harness, monkeypatch):
    run, out = harness

    class Early(World):
        def handle(self, action, args, stdin):
            if action == "tracked":
                os.kill(os.getpid(), signal.SIGTERM)
            return super().handle(action, args, stdin)

    monkeypatch.setattr(local_worker_fakes, "World", Early)
    with pytest.raises(KeyboardInterrupt):
        run([READ], "run")
    assert not list(out.glob("episodes-*.jsonl"))


@pytest.mark.parametrize("step", ["build_record", "append_episode"])
def test_a_signal_while_the_line_is_built_or_written_is_only_counted(harness, monkeypatch, step):
    """PR #248 review item 1: run() has returned; cli.py builds and appends the line."""
    run, out = harness
    owner = cli if step == "build_record" else corpus_io
    monkeypatch.setattr(owner, step, _sigterm_then(getattr(owner, step)))
    try:
        assert run([READ], "run") == 0
    except KeyboardInterrupt:
        pytest.fail("a signal after run() returned escaped the CLI")
    [rec] = lines(out)
    assert rec["verification"]["exit_reason"] == "COMPLETED"
    assert rec["termination"]["interrupted"] is False
    # the transcript is written after build_record and before append_episode
    assert _transcript(out, rec)["late_signals"] == (1 if step == "build_record" else 0)


@pytest.mark.parametrize(("limits", "stop"), [({}, "COMPLETED"), ({"max_steps": 1}, "TURN_LIMIT")])
def test_a_first_signal_during_cleanup_is_only_counted(tmp_path, on_sigterm, limits, stop):
    """PR #248 review item 6: no signal before; the loop had stopped (the model finished, or a
    limit) when the sweep runs."""

    class SweepSignal(World):
        def handle(self, action, args, stdin):
            if action == "sweep":
                os.kill(os.getpid(), signal.SIGTERM)
            return super().handle(action, args, stdin)

    loop, world, _, _ = make_loop(tmp_path, [READ], world=SweepSignal(), **limits)
    on_sigterm(loop)
    out = _run(loop)
    assert out.exit_reason == stop and not out.interrupted and loop.deferred is None
    assert out.cleanup == "ok" and out.patch == (0, 0, 0) and loop.late_signals == 1
    assert [c[0] for c in world.calls][-2:] == ["sweep", "numstat"]  # the cleanup went on
    validate_record(episode.build_record(META, out))
