"""#88 local worker loop: fixes for the Codex review of #239 (REJECT, blocking items B1-B8)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from local_worker_fakes import META, TASK, cli_harness, lines, make_loop, reply

from experimental.local_worker import agent, episode
from experimental.local_worker.agent import initial_messages
from experimental.local_worker.episode_schema import SchemaError, validate_record

EDIT_SRC = reply("edit_file", path="src/app.py", target="x = 1", replacement="x = 2")
SECRET = "CONFIDENTIAL-model-text-4e1"


@pytest.fixture
def harness(tmp_path, monkeypatch):
    return cli_harness(tmp_path, monkeypatch)


def _record(tmp_path, replies, world=None, **limits):
    loop, world, endpoint, clock = make_loop(tmp_path, replies, world, **limits)
    out = loop.run(initial_messages(TASK))
    rec = episode.build_record(META, out)
    validate_record(rec)
    return rec, world


def raw_call(function: Any, call_id: Any = "c0") -> dict:
    msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": call_id, "function": function}],
    }
    return {
        "model": "served-model",
        "choices": [{"message": msg}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


# -- B1: no model text through arguments a tool does not take, or through a path never used ----
def test_an_unused_argument_is_a_recorded_protocol_abort(harness):
    run, out = harness
    finish = reply("finish", status="DONE", path=SECRET)
    assert run([EDIT_SRC, finish], "run") == 0
    [rec] = lines(out)
    assert rec["verification"]["exit_reason"] == "ERROR_ABORT"
    assert rec["termination"]["abort_cause"] == "protocol_error"
    assert rec["turns"][-1]["action_type"] == "invalid_tool_call"
    assert rec["turns"][-1]["error_category"] == "PROTOCOL_ERROR"
    assert rec["termination"]["cleanup"] == "ok" and rec["verification"]["patch_lines_added"] == 1
    assert SECRET not in json.dumps(rec)


def test_a_path_that_named_nothing_is_published_only_as_a_digest(tmp_path):
    rec, _ = _record(tmp_path, [reply("read_file", path=SECRET), reply("list_dir", path="src")])
    first, second = rec["turns"][0], rec["turns"][1]
    assert first["exit_code"] == 64 and set(first["tool_args"]["path"]) == {"sha256", "bytes"}
    assert second["tool_args"] == {"path": "src"}  # listed: a real directory
    assert SECRET not in json.dumps(rec)
    plain = {**first, "tool_args": {"path": SECRET}}
    with pytest.raises(SchemaError, match="successful call"):
        validate_record({**rec, "turns": [plain, *rec["turns"][1:]]})
    finish = {**rec["turns"][-1], "tool_args": {"status": "DONE", "path": "src"}}
    with pytest.raises(SchemaError, match="not a public argument"):
        validate_record({**rec, "turns": [*rec["turns"][:-1], finish]})


# -- B8: malformed calls are recorded aborts; the line and the cleanup always happen -----------
@pytest.mark.parametrize(
    "function",
    [
        {"name": [], "arguments": "{}"},
        {"name": {}, "arguments": "{}"},
        {"name": "read_file", "arguments": 7},
        "read_file",
    ],
)
def test_a_malformed_call_is_a_recorded_abort(harness, function):
    run, out = harness
    assert run([EDIT_SRC, raw_call(function)], "run") == 0
    [rec] = lines(out)
    assert rec["termination"]["abort_cause"] == "protocol_error"
    assert rec["termination"]["cleanup"] == "ok" and rec["verification"]["patch_lines_added"] == 1


def test_a_non_string_call_id_is_a_protocol_error(tmp_path):
    rec, _ = _record(
        tmp_path, [raw_call({"name": "finish", "arguments": '{"status": "DONE"}'}, [1])]
    )
    assert rec["termination"]["abort_cause"] == "protocol_error"


def test_an_unexpected_error_is_recorded_and_cleaned_up(tmp_path, monkeypatch):
    def boom(call: Any, n: int) -> Any:
        raise LookupError("unexpected")

    monkeypatch.setattr(agent, "decode_call", boom)
    rec, world = _record(tmp_path, [EDIT_SRC])
    assert rec["verification"]["exit_reason"] == "ERROR_ABORT"
    assert rec["termination"]["abort_cause"] == "internal_error"
    assert [c[0] for c in world.calls][-2:] == ["sweep", "numstat"]
