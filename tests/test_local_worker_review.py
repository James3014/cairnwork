"""#88 local worker loop: fixes from the independent review of #239 (findings 1-10)."""

from __future__ import annotations

import pytest
from local_worker_fakes import cli_harness, lines, reply

from experimental.local_worker import episode

EDIT_SRC = reply("edit_file", path="src/app.py", target="x = 1", replacement="x = 2")


@pytest.fixture
def harness(tmp_path, monkeypatch):
    return cli_harness(tmp_path, monkeypatch)


def transcript_of(out, episode_id):
    return out / "private" / episode_id / "transcript.json"


def test_resume_from_a_turn_3_cut_uses_one_basis(harness):
    """Finding 1: the child's cut fields and times are its own; the parent's prefix is only in
    parent_pre_cut_*; wall_clock_seconds = parent pre-cut seconds + own seconds."""
    run, out = harness
    parent_replies = [
        reply("read_file", usage=(100, 10, 50), path="src/app.py"),
        reply("list_dir", usage=(200, 20, 100), path="."),
        reply("read_file", usage="missing", path="src/app.py"),
        EDIT_SRC,  # turn 3: the cut
        reply("finish", status="DONE"),
    ]
    assert run(parent_replies, "run") == 0
    [parent] = lines(out)
    assert parent["first_edit_cut"]["cut_turn_index"] == 3
    child_replies = [
        reply("read_file", path="src/app.py"),
        EDIT_SRC,
        reply("finish", status="DONE"),
    ]
    tr = str(transcript_of(out, parent["episode_id"]))
    assert run(child_replies, "resume", "--seed", "1000", "--parent-transcript", tr) == 0
    child = lines(out)[1]
    assert child["parent_pre_cut_read_actions"] == 3
    assert child["parent_pre_cut_tokens_accum"] is None  # turn 2 had no usage
    assert child["parent_pre_cut_turns_missing_usage"] == 1
    assert child["parent_pre_cut_wall_seconds"] == parent["first_edit_cut"]["pre_cut_wall_seconds"]
    cut = child["first_edit_cut"]
    assert cut["cut_turn_index"] == 4  # child turns are numbered on from the cut (3, 4, 5)
    assert cut["pre_cut_read_actions"] == 1  # only the child's own read
    assert cut["pre_cut_tokens_accum"] == 160 and cut["pre_cut_turns_missing_usage"] == 0
    assert child["turns"][0]["turn_index"] == 3 and child["turns"][0]["t_start_seconds"] < 0.5
    assert child["wall_clock_seconds"] == pytest.approx(
        child["parent_pre_cut_wall_seconds"] + child["own_wall_seconds"], abs=0.002
    )
    assert parent["parent_pre_cut_wall_seconds"] is None  # a fresh run has no parent fields


def test_resume_refuses_a_resume_parent(harness):
    run, out = harness
    run([reply("read_file", path="src/app.py"), EDIT_SRC], "run")
    [parent] = lines(out)
    tr = str(transcript_of(out, parent["episode_id"]))
    assert run([reply("finish", status="DONE")], "resume", "--parent-transcript", tr) == 0
    child = lines(out)[1]
    tr2 = str(transcript_of(out, child["episode_id"]))
    assert run([], "resume", "--parent-transcript", tr2, "--cut-turn", "1") == 2


def test_cut_900s_budget_shrinks_by_the_parent_prefix():
    turns = [{"t_end_seconds": 50.0, "tree_differs_from_base": True}]
    got = episode.cut_at(turns, "COMPLETED", 60.0, max(0.0, 900.0 - 870.0))
    assert got["limit_seconds"] == 30.0 and got["within_limit"] is False
    assert got["exit_reason"] == "TIMEOUT" and got["turns_completed"] == 0
