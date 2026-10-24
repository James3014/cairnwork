"""What the local worker's model sees and may call (#88): the system prompt, the tool schemas, and
the decoding of a tool call. ``run_tests`` takes no free-form arguments (Plan v2 point 2): the loop
runs ``pytest -q --tb=short -- <targets>`` over the task's declared public targets, or the one the
model picks by ``target_index``.
"""

from __future__ import annotations

import json
from typing import Any

from experimental.local_worker.tools import ToolCall

RUN_TESTS_ARGS = ("-q", "--tb=short", "--")
NUDGE = "No tool call received. Reply with exactly one tool call, or call finish."
SYSTEM_PROMPT = (
    "You are fixing one task in a Python repository. You act only through the tools list_dir, "
    "read_file, write_file, edit_file, run_tests and finish; paths are relative to the repository "
    "root. Call exactly one tool per reply. run_tests runs the task's public tests (all of them, "
    "or the one named by target_index). When your change is complete, call finish with status "
    "DONE. If the task cannot be done because its specification lacks a fact (a name, a contract "
    "or the expected behaviour), call finish with status NEEDS_SPEC and say what is missing."
)


def _fn(name: str, desc: str, props: dict, required: list[str]) -> dict:
    params = {"type": "object", "properties": props, "required": required}
    spec = {"name": name, "description": desc, "parameters": params}
    return {"type": "function", "function": spec}


_S = {"type": "string"}
TOOL_SCHEMAS = [
    _fn("list_dir", "List a directory of the repository.", {"path": _S}, ["path"]),
    _fn("read_file", "Print a file of the repository.", {"path": _S}, ["path"]),
    _fn("write_file", "Create or replace.", {"path": _S, "content": _S}, ["path", "content"]),
    _fn(
        "edit_file",
        "Replace the single exact occurrence of target in a file.",
        {"path": _S, "target": _S, "replacement": _S},
        ["path", "target", "replacement"],
    ),
    _fn(
        "run_tests",
        "Run the task's public tests: all of them, or one by index.",
        {"target_index": {"type": "integer"}},
        [],
    ),
    _fn(
        "finish",
        "End the task.",
        {"status": {"type": "string", "enum": ["DONE", "NEEDS_SPEC"]}, "summary": _S},
        ["status"],
    ),
]
TOOLS_ABSENT = ("search",)  # the wrapper has no search action (Plan v1 design item 1: declared)
_REQUIRED = {
    "list_dir": ("path",),
    "read_file": ("path",),
    "write_file": ("path", "content"),
    "edit_file": ("path", "target", "replacement"),
    "run_tests": (),
    "finish": ("status",),
}


ALLOWED_ARGS = {
    t["function"]["name"]: set(t["function"]["parameters"]["properties"]) for t in TOOL_SCHEMAS
}


class BadCall(ValueError):
    """A tool call whose argument values the loop cannot run (unknown tool name, arguments that
    are not a JSON object, a missing or ill-typed argument): the model is told why, the turn is
    recorded as ``invalid_tool_call`` and nothing executes."""


class ProtocolError(ValueError):
    """A reply that breaks the tool-call protocol itself (a call or function that is not an
    object, a tool name or id that is not a string, arguments that are neither a JSON string nor
    an object, or an argument the tool does not take): the episode stops with a recorded
    ERROR_ABORT (abort_cause protocol_error); nothing executes and no argument is published."""


def _arguments(fn: dict) -> Any:
    raw = fn.get("arguments")
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        raise ProtocolError("arguments are neither a JSON string nor an object")
    try:
        return json.loads(raw)
    except ValueError:
        raise BadCall("arguments are not valid JSON") from None


def decode_call(call: Any, n_targets: int) -> tuple[str, dict]:
    """(tool name, arguments) of one call; raises ProtocolError or BadCall (see above)."""
    if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
        raise ProtocolError("a tool call must be an object with a function object")
    if call.get("id") is not None and not isinstance(call["id"], str):
        raise ProtocolError("a tool call id must be a string")
    fn = call["function"]
    name = fn.get("name")
    if not isinstance(name, str):
        raise ProtocolError("a tool name must be a string")
    if name not in _REQUIRED:
        raise BadCall("unknown tool")
    args = _arguments(fn)
    if not isinstance(args, dict):
        raise BadCall("arguments must be a JSON object")
    extra = set(args) - ALLOWED_ARGS[name]
    if extra:  # their names and values are model text: neither is kept in the public line
        raise ProtocolError(f"{name} got {len(extra)} argument(s) it does not take")
    for key in _REQUIRED[name]:
        if not isinstance(args.get(key), str):
            raise BadCall(f"{name} needs a string {key!r}")
    if name == "finish" and args["status"] not in ("DONE", "NEEDS_SPEC"):
        raise BadCall("finish status must be DONE or NEEDS_SPEC")
    if name == "finish" and not isinstance(args.get("summary", ""), str):
        raise BadCall("finish summary must be a string")
    index = args.get("target_index")
    if name == "run_tests" and index is not None:
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < n_targets:
            raise BadCall(f"target_index must be an integer in [0, {n_targets})")
    return name, args


def render(call: ToolCall) -> str:
    rc = "timeout" if call.exit_code is None else str(call.exit_code)
    note = ", output truncated" if call.stdout_truncated else ""
    return f"[exit {rc}{note}]\n{call.output}"
