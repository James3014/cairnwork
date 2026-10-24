"""cut_900s with a turn that spans its limit (Codex review of PR #248, MUST-FIX 2; since #239).

cut_at kept only the turns ended by the limit, so a tool-running turn that started before it and
ended after it was dropped and tree_differs_at_limit reported the earlier state, which nobody
observed at the limit. It is now null then; turns_completed still counts only the turns ended by
the limit, and the validator recomputes both the same way.
"""

from __future__ import annotations

import pytest
from local_worker_fakes import META, TASK, World, make_loop, reply

from experimental.local_worker import episode
from experimental.local_worker.agent import initial_messages
from experimental.local_worker.episode_schema import SchemaError, validate_record


def _turn(start, end, ran, differs):
    return {
        "t_start_seconds": start,
        "t_end_seconds": end,
        "tool_ran": ran,
        "tree_differs_from_base": differs if ran else None,
    }


def test_a_write_turn_spanning_the_limit_leaves_the_tree_unknown():
    """The review's example: a turn starts at 800 s, writes at 850 s, ends at 950 s."""
    turns = [_turn(10.0, 20.0, True, False), _turn(800.0, 950.0, True, True)]
    cut = episode.cut_at(turns, "COMPLETED", 960.0, 900.0)
    assert cut["tree_differs_at_limit"] is None  # was False: the state before that turn
    assert cut["turns_completed"] == 1 and cut["exit_reason"] == "TIMEOUT"


@pytest.mark.parametrize(
    ("turn", "want", "completed"),
    [
        (_turn(800.0, 950.0, False, None), False, 1),  # no tool ran in it: nothing could change
        (_turn(800.0, 900.0, True, True), True, 2),  # ends exactly at the limit: observed by then
        (_turn(900.0, 950.0, True, True), False, 1),  # starts at the limit: nothing before it
        (_turn(950.0, 990.0, True, True), False, 1),  # wholly after the limit
    ],
)
def test_only_a_tool_turn_strictly_spanning_the_limit_is_unknown(turn, want, completed):
    turns = [_turn(10.0, 20.0, True, False), turn]
    cut = episode.cut_at(turns, "COMPLETED", 999.0, 900.0)
    assert cut["tree_differs_at_limit"] is want and cut["turns_completed"] == completed


def _spanning_record(tmp_path):
    """A real run whose write turn takes 20 s; the cut limit falls in the middle of it."""
    world = World()
    world.tool_seconds = 5.0  # every wrapper call takes 5 s on the fake clock
    loop, _, _, _ = make_loop(
        tmp_path, [reply("write_file", path="src/new.py", content="y\n")], world
    )
    out = loop.run(initial_messages(TASK))
    write = out.turns[0]
    assert write.tool_ran and write.t_end - write.t_start >= 10.0
    return out, (write.t_start + write.t_end) / 2


def test_a_real_run_cut_inside_its_write_turn(tmp_path):
    out, middle = _spanning_record(tmp_path)
    rec = episode.build_record(META, out, cut_seconds=middle)
    validate_record(rec)
    cut = rec["cut_900s"]
    assert cut["tree_differs_at_limit"] is None and cut["turns_completed"] == 0
    bad = dict(rec, cut_900s=dict(cut, tree_differs_at_limit=False))  # the old answer
    with pytest.raises(SchemaError, match="cut_900s differs"):
        validate_record(bad)
    end = round(out.turns[0].t_end, 3)
    rec = episode.build_record(META, out, cut_seconds=end)
    validate_record(rec)
    assert rec["cut_900s"]["tree_differs_at_limit"] is True
    assert rec["cut_900s"]["turns_completed"] == 1
