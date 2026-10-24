"""Tool-call decoding after the round-2 review of #239: an extra or unknown argument is a
recoverable invalid_tool_call (NB6), structural breaks still abort, and the assistant's tool
call and the tool reply always share one id (NB7)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from local_worker_fakes import META, TASK, make_loop, reply

from experimental.local_worker import episode
from experimental.local_worker.agent import initial_messages
from experimental.local_worker.episode_schema import validate_record

SECRET = "EXTRA-ARGUMENT-TEXT-8c2"


def _raw(calls: Any) -> dict:
    msg = {"role": "assistant", "content": None, "tool_calls": calls}
    usage = {"prompt_tokens": 1, "completion_tokens": 1}
    return {"model": "served-model", "choices": [{"message": msg}], "usage": usage}


def _fn(name: Any, **args: Any) -> dict:
    return {"name": name, "arguments": json.dumps(args)}


def _run(tmp_path, replies):
    loop, world, endpoint, _ = make_loop(tmp_path, replies)
    out = loop.run(initial_messages(TASK))
    rec = episode.build_record(META, out)
    validate_record(rec)
    return rec, world, endpoint


def _pairs(messages: list[dict]) -> list[tuple[str, str]]:
    """(assistant call id, the tool reply's id) for every assistant message with a tool call."""
    out = []
    for i, m in enumerate(messages):
        if m["role"] == "assistant" and m.get("tool_calls"):
            out.append((m["tool_calls"][0]["id"], messages[i + 1]["tool_call_id"]))
    return out


def test_an_extra_argument_is_an_invalid_call_and_the_episode_goes_on(tmp_path):
    bad = reply("write_file", path="src/app.py", content="x = 9\n", mode=SECRET)
    rec, world, endpoint = _run(tmp_path, [bad, reply("read_file", path="src/app.py")])
    assert world.files["src/app.py"] == "x = 1\n"  # nothing executed
    assert [c[0] for c in world.calls].count("write") == 0
    first = rec["turns"][0]
    assert (first["action_type"], first["error_category"]) == ("invalid_tool_call", "INVALID_CALL")
    assert first["tool_args"] == {} and first["tool_ran"] is False and first["exit_code"] is None
    assert [t["action_type"] for t in rec["turns"]] == ["invalid_tool_call", "read_file", "finish"]
    assert rec["verification"]["exit_reason"] == "COMPLETED"
    told = endpoint.payloads[1]["messages"][-1]
    assert told["role"] == "tool" and "does not take" in told["content"]
    assert SECRET not in told["content"] and SECRET not in json.dumps(rec)


@pytest.mark.parametrize(
    "call",
    [
        {"function": _fn("no_such_tool", path="x")},  # unknown tool
        {"function": _fn("read_file")},  # missing argument
        {"function": _fn("read_file", path="src/app.py", extra=SECRET)},  # extra argument
        {"id": "", "function": _fn("read_file", path="src/app.py", extra=SECRET)},
    ],
)
def test_a_bad_call_without_an_id_gets_one_id_on_both_sides(tmp_path, call):
    rec, _, endpoint = _run(tmp_path, [_raw([call])])
    assert rec["turns"][0]["action_type"] == "invalid_tool_call"
    messages = endpoint.payloads[1]["messages"]
    assert _pairs(messages) == [("call_0", "call_0")]


def test_a_bad_call_keeps_the_models_own_id(tmp_path):
    call = {"id": "abc", "function": _fn("read_file", path="src/app.py", extra=SECRET)}
    _, _, endpoint = _run(tmp_path, [_raw([call])])
    assert _pairs(endpoint.payloads[1]["messages"]) == [("abc", "abc")]


@pytest.mark.parametrize(
    "calls",
    [
        {"function": _fn("read_file", path="a")},  # tool_calls is not a list
        ["read_file"],  # a call that is not an object
        [{"function": "read_file"}],  # a function that is not an object
        [{"function": _fn(7, path="a")}],  # a tool name that is not a string
        [{"id": 0, "function": _fn("read_file", path="a")}],  # an id that is not a string
        [{"function": {"name": "read_file", "arguments": 5}}],  # arguments of neither kind
    ],
)
def test_a_structurally_broken_call_still_aborts(tmp_path, calls):
    rec, world, _ = _run(tmp_path, [_raw(calls)])
    assert rec["verification"]["exit_reason"] == "ERROR_ABORT"
    assert rec["termination"]["abort_cause"] == "protocol_error"
    assert rec["turns"][0]["error_category"] == "PROTOCOL_ERROR"
    assert [c[0] for c in world.calls].count("read") == 0
