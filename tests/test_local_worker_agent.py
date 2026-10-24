"""experimental.local_worker.agent: the loop over fake tools and a fake endpoint (#88, Plan v2)."""

from __future__ import annotations

from typing import Any

import pytest
from local_worker_fakes import BASE_SHA, TASK, World, make_loop, reply

from experimental.local_worker import agent, episode
from experimental.local_worker.agent import RefusedStart, initial_messages
from experimental.local_worker.endpoint import EndpointError, EndpointTimeout
from experimental.local_worker.episode import build_record, first_edit_cut
from experimental.local_worker.episode_schema import validate_record

META: dict[str, Any] = {
    "episode_id": "ep_test",
    "task_id": "TASK_01",
    "repo": "repo_x",
    "model_name": "served-model",
    "harness_version": "0" * 40,
    "checkout_id": "0" * 32,
    "arm": "L0",
    "seed": 7,
    "resume_parent_episode_id": None,
    "resume_cut_turn_index": None,
    "plan_sha256": None,
    "endpoint": "http://127.0.0.1:8000/v1",
    "server_model_id": "served-model",
    "manifest_sha256": "1" * 64,
    "canary_report_sha256": "2" * 64,
}
EDIT_SRC = reply("edit_file", path="src/app.py", target="x = 1", replacement="x = 2")


def run(tmp_path, replies, world=None, **kw):
    loop, world, endpoint, clock = make_loop(tmp_path, replies, world, **kw)
    out = loop.run(initial_messages(TASK))
    rec = build_record(META, out)
    validate_record(rec)
    return out, rec, world, endpoint


def cut_of(tmp_path, replies, world=None) -> dict:
    return run(tmp_path, replies, world)[1]["first_edit_cut"]


def test_cut_at_first_source_edit_counts_reads_before_it(tmp_path):
    replies = [reply("read_file", path="src/app.py"), reply("list_dir", path="."), EDIT_SRC]
    cut = cut_of(tmp_path, replies)
    assert cut["cut_occurred"] and cut["cut_turn_index"] == 2
    assert cut["target_file"] == "src/app.py" and cut["is_test_file"] is False
    assert cut["cut_cause"] == "edit_file" and cut["cut_ambiguous"] is False
    assert cut["pre_cut_read_actions"] == 2
    assert cut["changed_paths"] == ["src/app.py"]


def test_a_test_file_write_is_the_cut(tmp_path):
    """Plan v2 point 4: any tree change cuts, tests included (the tree is no longer base)."""
    replies = [
        reply("read_file", path="src/app.py"),
        reply("write_file", path="tests/test_new.py", content="def test_x(): pass\n"),
        EDIT_SRC,
    ]
    cut = cut_of(tmp_path, replies)
    assert cut["cut_turn_index"] == 1 and cut["target_file"] == "tests/test_new.py"
    assert cut["is_test_file"] is True and cut["cut_cause"] == "write_file"


def test_a_doc_file_write_is_the_cut(tmp_path):
    cut = cut_of(tmp_path, [reply("write_file", path="docs/notes.md", content="n\n"), EDIT_SRC])
    assert cut["cut_turn_index"] == 0 and cut["target_file"] == "docs/notes.md"
    assert cut["is_test_file"] is False


def test_no_edit_means_no_cut_and_null_fields(tmp_path):
    cut = cut_of(tmp_path, [reply("read_file", path="src/app.py")])
    assert cut["cut_occurred"] is False
    assert all(cut[k] is None for k in episode.CUT_FIELDS[1:])


def test_edit_at_turn_zero_has_observed_zero_counts(tmp_path):
    cut = cut_of(tmp_path, [EDIT_SRC])
    assert cut["cut_turn_index"] == 0
    assert cut["pre_cut_read_actions"] == 0 and cut["pre_cut_tokens_accum"] == 0
    assert cut["pre_cut_wall_seconds"] == 0.0


def test_run_tests_side_effect_cuts_and_is_flagged(tmp_path):
    world = World()
    world.pytest_writes = {"src/generated.py": "g = 1\n"}
    cut = cut_of(tmp_path, [reply("read_file", path="src/app.py"), reply("run_tests")], world)
    assert cut["cut_turn_index"] == 1 and cut["cut_cause"] == "run_tests_side_effect"
    assert cut["cut_ambiguous"] is True and cut["target_file"] == "src/generated.py"


def test_a_refused_write_does_not_cut(tmp_path):
    cut = cut_of(tmp_path, [reply("write_file", path="../x.py", content="x"), EDIT_SRC])
    assert cut["cut_turn_index"] == 1


def test_status_runs_after_every_tool_call(tmp_path):
    _, rec, world, _ = run(tmp_path, [reply("read_file", path="src/app.py"), EDIT_SRC])
    actions = [c[0] for c in world.calls]
    assert actions == ["status", "read", "status", "read", "write", "status", "sweep", "numstat"]
    assert [t["tree_differs_from_base"] for t in rec["turns"]] == [False, True, None]


def test_tokens_accumulate_before_the_cut_and_totals_are_sums(tmp_path):
    replies = [
        reply("read_file", usage=(100, 10, 50), path="src/app.py"),
        reply("read_file", usage=(200, 20, 150), path="src/app.py"),
        reply("edit_file", usage=(300, 30, 250), path="src/app.py", target="x", replacement="y"),
    ]
    _, rec, _, _ = run(tmp_path, replies)
    assert rec["first_edit_cut"]["pre_cut_tokens_accum"] == 100 + 10 + 50 + 200 + 20 + 150
    finish = (100, 10, 50)  # the default finish reply
    assert rec["tokens_total"] == {
        "prompt": 600 + finish[0],
        "completion": 60 + finish[1],
        "cached": 450 + finish[2],
    }
    assert rec["usage_status"] == "ok"


def test_missing_usage_is_null_never_zero(tmp_path):
    replies = [reply("read_file", usage="missing", path="src/app.py"), EDIT_SRC]
    _, rec, _, _ = run(tmp_path, replies)
    t0 = rec["turns"][0]
    assert (t0["tokens_prompt"], t0["tokens_completion"], t0["tokens_cached"]) == (None,) * 3
    assert t0["usage_status"] == "missing" and rec["usage_status"] == "partial"
    assert rec["tokens_total"]["prompt"] is None
    assert rec["first_edit_cut"]["pre_cut_tokens_accum"] is None


def test_absent_cached_field_is_null_with_its_own_status(tmp_path):
    _, rec, _, _ = run(tmp_path, [reply("read_file", usage=(5, 1, None), path="src/app.py")])
    t0 = rec["turns"][0]
    assert t0["tokens_cached"] is None and t0["usage_status"] == "no_cache_field"
    assert t0["tokens_prompt"] == 5


def test_step_limit(tmp_path):
    replies = [reply("read_file", path="src/app.py")] * 10
    out, rec, _, _ = run(tmp_path, replies, max_steps=3)
    assert out.exit_reason == "TURN_LIMIT" and out.limit_hit == "max_steps"
    assert rec["total_turns"] == 3


def test_wall_clock_limit_uses_one_deadline(tmp_path):
    replies = [reply("read_file", path="src/app.py")] * 10
    out, rec, world, endpoint = run(
        tmp_path, replies, max_wall_seconds=1000, cleanup_reserve_seconds=60, model_seconds=400
    )
    assert out.exit_reason == "TIMEOUT" and out.timed_out and out.limit_hit == "max_wall_seconds"
    assert rec["total_turns"] == 3  # starts at 0, 400, 800 (< 940); 1200 is past the deadline
    assert len(set(endpoint.deadlines)) == 1  # the same deadline for every request
    deadline = endpoint.deadlines[0]
    assert deadline == pytest.approx(1000.0 + 1000 - 60)
    tool_timeouts = [c[2] for c in world.calls if c[0] == "read"]
    assert tool_timeouts == [300.0, deadline - 1800.0]  # min(tool cap, time left)
    assert rec["turns"][2]["error_category"] == "TIMEOUT"  # replied, but its tool never ran
    assert rec["turns"][2]["exit_code"] is None
    assert rec["termination"]["cleanup"] == "ok"
    assert rec["cut_900s"]["exit_reason"] == "TIMEOUT"


def test_no_request_is_sent_after_the_deadline(tmp_path):
    world = World()
    world.tool_seconds = 100.0  # each wrapper call (tool or status) takes 100 s; model instant
    replies = [reply("read_file", path="src/app.py")] * 10
    out, rec, _, endpoint = run(
        tmp_path, replies, world, max_wall_seconds=1000, cleanup_reserve_seconds=60, model_seconds=0
    )
    assert out.exit_reason == "TIMEOUT" and rec["total_turns"] == 5
    assert len(endpoint.payloads) == 5  # the loop stops itself; it does not probe the endpoint


def test_context_cap(tmp_path):
    replies = [reply("read_file", usage=(950, 50, 0), path="src/app.py")] * 5
    out, rec, _, _ = run(tmp_path, replies, context_cap=1000)
    assert out.exit_reason == "TURN_LIMIT" and out.limit_hit == "context_cap"
    assert rec["total_turns"] == 1


def test_context_cap_without_usage_uses_a_byte_estimate(tmp_path):
    replies = [reply("read_file", usage="missing", path="src/app.py")] * 5
    out, _, _, _ = run(tmp_path, replies, context_cap=200)
    assert out.limit_hit == "context_cap_bytes_estimate"


def test_endpoint_timeout_and_error(tmp_path):
    out, rec, _, _ = run(tmp_path, [EndpointTimeout("slow")])
    assert out.exit_reason == "TIMEOUT" and rec["total_turns"] == 0
    out, rec, _, _ = run(tmp_path / "b", [EndpointError("HTTP 500")])
    assert out.exit_reason == "ERROR_ABORT" and out.abort_cause == "endpoint_error"


def test_interrupt_is_recorded_and_cleanup_still_runs(tmp_path):
    out, rec, world, _ = run(tmp_path, [KeyboardInterrupt()])
    assert out.interrupted and out.exit_reason == "ERROR_ABORT"
    assert [c[0] for c in world.calls][-2:] == ["sweep", "numstat"]


def test_status_failure_aborts(tmp_path):
    loop, world, _, _ = make_loop(tmp_path, [EDIT_SRC])
    world.calls.clear()
    out = None
    orig = world.handle

    def handle(action, args, stdin):
        if action == "status" and any(c[0] == "write" for c in world.calls):
            return 1, b"fatal\n"
        return orig(action, args, stdin)

    world.handle = handle
    out = loop.run(initial_messages(TASK))
    assert out.exit_reason == "ERROR_ABORT" and out.abort_cause == "status_unavailable"


def test_finish_needs_spec_and_patch_counts(tmp_path):
    out, rec, _, _ = run(tmp_path, [EDIT_SRC, reply("finish", status="NEEDS_SPEC", summary="s")])
    assert rec["verification"]["exit_reason"] == "COMPLETED"
    assert rec["spec_feedback"]["needs_spec"] is True
    v = rec["verification"]
    assert (v["patch_lines_added"], v["patch_lines_removed"]) == (1, 1)
    assert rec["verification"]["ground_truth_pass"] is None


def test_extra_and_invalid_tool_calls_run_nothing(tmp_path):
    replies = [
        reply("read_file", n_calls=3, path="src/app.py"),
        reply("run_tests", target_index=5),
        reply("delete_everything", path="/"),
        reply(None),
    ]
    _, rec, world, endpoint = run(tmp_path, replies)
    turns = rec["turns"]
    assert turns[0]["extra_tool_calls_ignored"] == 2
    assert [t["action_type"] for t in turns[1:4]] == ["invalid_tool_call"] * 2 + ["no_tool_call"]
    assert sum(c[0] == "pytest" for c in world.calls) == 0
    assert len(endpoint.payloads[1]["messages"][2]["tool_calls"]) == 1


def test_run_tests_runs_only_declared_targets(tmp_path):
    _, _, world, endpoint = run(tmp_path, [reply("run_tests"), reply("run_tests", target_index=0)])
    pytest_args = [c[1] for c in world.calls if c[0] == "pytest"]
    assert pytest_args == [["-q", "--tb=short", "--", "tests/test_app.py"]] * 2
    params = agent.TOOL_SCHEMAS[4]["function"]["parameters"]
    assert set(params["properties"]) == {"target_index"}
    first = endpoint.payloads[0]
    assert first["seed"] == 7 and first["parallel_tool_calls"] is False


def test_dirty_or_wrong_base_refuses_to_start(tmp_path):
    world = World()
    world.files["src/app.py"] = "dirty\n"
    loop, _, _, _ = make_loop(tmp_path, [], world)
    with pytest.raises(RefusedStart):
        loop.run(initial_messages(TASK))
    loop, _, _, _ = make_loop(tmp_path / "b", [])
    with pytest.raises(RefusedStart):
        loop.run(initial_messages(TASK), expected_base="c" * 40)


def test_resume_continues_from_the_prefix(tmp_path):
    loop, world, endpoint, clock = make_loop(tmp_path, [EDIT_SRC])
    prefix = initial_messages(TASK)
    prefix.append({"role": "assistant", "content": None, "tool_calls": []})
    prefix.append({"role": "user", "content": "PLAN"})
    out = loop.run(list(prefix), first_turn=4, elapsed_offset=120.0, expected_base=BASE_SHA)
    assert endpoint.payloads[0]["messages"] == prefix
    assert [t.index for t in out.turns] == [4, 5]
    assert out.turns[0].t_start == pytest.approx(120.0)
    assert first_edit_cut(out.turns, out.base)["cut_turn_index"] == 4
