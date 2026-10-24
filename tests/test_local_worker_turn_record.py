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


def _run(tmp_path, monkeypatch, action: str, at: int | None):
    events = Events(at)

    class Hooked(TurnLog):
        def __setattr__(self, name, value):
            super().__setattr__(name, value)
            events.tick(f"set {name}")

    class HookedWorld(World):
        def handle(self, action, args, stdin):
            events.tick(f"before {action}")
            result = super().handle(action, args, stdin)
            events.tick(f"after {action}")
            return result

    monkeypatch.setattr(agent, "TurnLog", Hooked)
    world = HookedWorld()
    loop, world, _, _ = make_loop(tmp_path, [REPLIES[action]], world=world)
    before = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, loop.interrupt)
    try:
        return loop.run(initial_messages(TASK)), world, events
    finally:
        signal.signal(signal.SIGTERM, before)


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
