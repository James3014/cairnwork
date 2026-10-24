"""A turn is recorded before it runs (Codex review of PR #248, MUST-FIX 1).

A SIGTERM landing after a write tool ran but before the turn reached ``out.turns`` lost the turn:
the patch counted the write while the line claimed no turn and NO_CUT. Now ``_loop`` appends the
turn first and ``run`` closes it; this sends the first SIGTERM at every point of a turn (each
TurnLog field the loop sets, before and after each wrapper call) and checks the line never
claims an unchanged tree for a checkout that changed.
"""

from __future__ import annotations

import os
import signal

import pytest
from local_worker_fakes import META, TASK, World, make_loop, reply

from experimental.local_worker import agent, episode
from experimental.local_worker.agent import initial_messages
from experimental.local_worker.episode import TurnLog
from experimental.local_worker.episode_schema import validate_record

REPLIES = {
    "write_file": reply("write_file", path="src/new.py", content="y = 1\n"),
    "edit_file": reply("edit_file", path="src/app.py", target="x = 1", replacement="x = 2"),
}


class Events:
    """Counts hook events; sends this process SIGTERM at event ``at`` (None: never)."""

    def __init__(self, at: int | None) -> None:
        self.at, self.n, self.where = at, 0, None
        self.first_turn: int | None = None  # the first event of the loop (before it: start)

    def tick(self, where: str) -> None:
        if self.first_turn is None and where.startswith("set "):
            self.first_turn = self.n
        if self.n == self.at:
            self.where = where
            os.kill(os.getpid(), signal.SIGTERM)
        self.n += 1


def _ticking(real, events: Events, name: str):
    def call(*args):
        result = real(*args)
        events.tick(f"returned {name}")
        return result

    return call


def _run(tmp_path, monkeypatch, action: str, at: int | None):
    events = Events(at)

    class Hooked(TurnLog):
        def __setattr__(self, name, value):
            super().__setattr__(name, value)
            events.tick(f"set {name}")

    class HookedWorld(World):
        changed_at: float | None = None  # when the checkout first left its base (own clock)

        def handle(self, action, args, stdin):
            events.tick(f"before {action}")
            result = super().handle(action, args, stdin)
            if self.changed_at is None and self.files != self.base:
                self.changed_at = loop._own()
            events.tick(f"after {action}")
            return result

    monkeypatch.setattr(agent, "TurnLog", Hooked)
    world = HookedWorld()
    world.tool_seconds = 1.0  # each wrapper call takes a second: turns span time
    loop, _, _, _ = make_loop(tmp_path, [REPLIES[action]], world=world)
    for name in ("_dispatch", "observe"):  # a signal at the earliest point after they return
        monkeypatch.setattr(loop, name, _ticking(getattr(loop, name), events, name))
    before = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, loop.interrupt)
    try:
        return loop.run(initial_messages(TASK)), world, events
    finally:
        signal.signal(signal.SIGTERM, before)


def _check_every_limit(out, changed_at: float | None, where: str) -> None:
    """At every limit (quarter seconds over the run), cut_900s may claim only what was true
    then: True only once the checkout had changed, False only while it had not."""
    for k in range(int(out.wall_seconds * 4) + 8):
        limit = k / 4
        rec = episode.build_record(META, out, cut_seconds=limit)
        validate_record(rec)
        claim = rec["cut_900s"]["tree_differs_at_limit"]
        if claim is True:
            assert changed_at is not None and changed_at <= limit, (where, limit)
        elif claim is False:
            assert changed_at is None or changed_at > limit, (where, limit)


@pytest.mark.parametrize("action", sorted(REPLIES))
def test_a_signal_at_any_point_of_a_turn_never_hides_a_change(tmp_path, monkeypatch, action):
    _, _, dry = _run(tmp_path / "dry", monkeypatch, action, None)
    assert dry.first_turn is not None and dry.n > dry.first_turn + 20
    for at in range(dry.first_turn):  # base observe, tracked query: escapes, no line (run doc)
        with pytest.raises(KeyboardInterrupt):
            _run(tmp_path / str(at), monkeypatch, action, at)
    for at in range(dry.first_turn, dry.n):  # every point of the turns and of the cleanup
        try:
            out, world, events = _run(tmp_path / str(at), monkeypatch, action, at)
        except KeyboardInterrupt:
            pytest.fail(f"a signal at event {at} escaped run()")
        where = f"signal at event {at} ({events.where})"
        rec = episode.build_record(META, out)
        validate_record(rec)
        changed = world.files != world.base
        assert out.patch is not None and (out.patch[0] > 0) == changed, where
        if changed:  # the line may not claim the tree stayed at base
            assert rec["first_edit_cut"]["cut_status"] != "NO_CUT", where
            ran = [t for t in rec["turns"] if t["tool_ran"]]
            assert ran and ran[-1]["tree_differs_from_base"] is not False, where
        _check_every_limit(out, world.changed_at, where)


def test_a_finished_turn_keeps_its_own_end_time(tmp_path, monkeypatch):
    """Only the turn an interrupt or error stopped is ended by run(); a completed turn's t_end is
    set once, when it ended (run() must not overwrite it with the cleanup's start)."""
    sets: dict[int, int] = {}

    class Counting(TurnLog):
        def __setattr__(self, name, value):
            super().__setattr__(name, value)
            if name == "t_end" and value != 0.0:
                sets[self.index] = sets.get(self.index, 0) + 1

    monkeypatch.setattr(agent, "TurnLog", Counting)
    loop, _, _, _ = make_loop(tmp_path, [REPLIES["write_file"]])
    out = loop.run(initial_messages(TASK))
    assert out.exit_reason == "COMPLETED" and len(out.turns) == 2
    assert sets == {0: 1, 1: 1} and loop._open is None


def test_tool_ran_is_set_before_the_wrapper_runs(tmp_path):
    """Codex review of PR #248, round 2: tool_ran is pessimistic, True before the wrapper is
    invoked, so no interrupt point after the wrapper started can leave it False."""
    seen = []

    class Watching(World):
        def handle(self, action, args, stdin):
            if action == "write":
                seen.append(loop._open is not None and loop._open.tool_ran)
            return super().handle(action, args, stdin)

    loop, _, _, _ = make_loop(tmp_path, [REPLIES["write_file"]], world=Watching())
    out = loop.run(initial_messages(TASK))
    assert seen == [True] and out.turns[0].tool_ran is True


def test_a_signal_as_the_wrapper_returns_keeps_the_turn_and_its_change(tmp_path, monkeypatch):
    """A real SIGTERM at the earliest point after the write wrapper returned."""
    loop, world, _, _ = make_loop(tmp_path, [REPLIES["write_file"]])
    real = loop.tools.write_file

    def write_file(*args):
        call = real(*args)
        os.kill(os.getpid(), signal.SIGTERM)
        return call

    monkeypatch.setattr(loop.tools, "write_file", write_file)
    before = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, loop.interrupt)
    try:
        out = loop.run(initial_messages(TASK))
    finally:
        signal.signal(signal.SIGTERM, before)
    rec = episode.build_record(META, out)
    validate_record(rec)
    assert out.abort_cause == "interrupted" and rec["turns"][0]["tool_ran"] is True
    assert world.files != world.base and rec["first_edit_cut"]["cut_status"] != "NO_CUT"
