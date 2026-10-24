"""#88 episode line validator: value bounds, digest objects and cross-field rules (Codex review
of #239, non-blocking items). Each case corrupts one value of a valid line."""

from __future__ import annotations

import copy
import math
from typing import Any

import pytest
from local_worker_fakes import META, TASK, make_loop, reply

from experimental.local_worker import episode
from experimental.local_worker.agent import initial_messages
from experimental.local_worker.episode_schema import SchemaError, validate_record

WRITE = reply("write_file", path="src/app.py", content="x = 2\n")


@pytest.fixture(scope="module")
def line(tmp_path_factory) -> dict:
    loop, _, _, _ = make_loop(tmp_path_factory.mktemp("rules"), [WRITE])
    rec = episode.build_record(META, loop.run(initial_messages(TASK)))
    validate_record(rec)
    assert [t["action_type"] for t in rec["turns"]] == ["write_file", "finish"]
    return rec


def _set(rec: dict, path: str, value: Any) -> None:
    *head, last = path.split(".")
    node: Any = rec
    for key in head:
        node = node[int(key)] if isinstance(node, list) else node[key]
    node[int(last) if isinstance(node, list) else last] = value


DIGEST = "turns.0.tool_args.content"
CASES = [
    (f"{DIGEST}.bytes", -1, "content"),
    (f"{DIGEST}.bytes", True, "content"),
    (f"{DIGEST}.sha256", "z" * 64, "content"),
    (f"{DIGEST}.sha256", "ab", "content"),
    (DIGEST, {"sha256": "a" * 64, "bytes": 1, "text": "x"}, "content"),
    ("turns.0.stdout_bytes", -5, "non-negative"),
    ("turns.0.duration_ms", math.nan, "finite"),
    ("own_wall_seconds", math.inf, "finite"),
    ("first_edit_cut.changed_paths", [7], "changed_paths entries"),
    ("turns.0.error_category", "WAT", "error_category"),
    ("termination.abort_cause", "boom", "abort_cause"),
    ("termination.cleanup", "maybe", "cleanup"),
    ("termination.limit_hit", "budget", "limit_hit"),
    ("usage_status", "most", "usage_status"),
    ("turns.1.t_start_seconds", 0.0, "out of order"),
    ("turns.1.t_end_seconds", 999.0, "after the run ended"),
    ("turns.1.exec_identity", "_cairnwork_worker", "never ran"),
    ("termination.timed_out", True, "timed_out"),
    ("termination.interrupted", True, "interrupted"),
    ("spec_feedback.needs_spec", True, "needs_spec"),
    ("verification.patch_lines_removed", None, "null together"),
    ("cut_900s.turns_completed", 1, "cut_900s"),
    ("cut_900s.tree_differs_at_limit", False, "cut_900s"),
    ("cut_900s.exit_reason", "TIMEOUT", "cut_900s.exit_reason"),
    ("cut_900s.within_limit", False, "within_limit"),
    ("resume_cut_turn_index", 0, "go together"),
    ("plan_sha256", "c" * 64, "plan"),
]


@pytest.mark.parametrize(("path", "value", "match"), CASES)
def test_one_bad_value_is_refused(line, path, value, match):
    rec = copy.deepcopy(line)
    _set(rec, path, value)
    with pytest.raises(SchemaError, match=match):
        validate_record(rec)


def test_exit_reason_and_termination_must_agree(line):
    for changes in (
        {"abort_cause": "endpoint_error"},  # set, but the exit is COMPLETED
        {"limit_hit": "max_steps"},
    ):
        rec = copy.deepcopy(line)
        rec["termination"].update(changes)
        with pytest.raises(SchemaError, match="termination"):
            validate_record(rec)
    rec = copy.deepcopy(line)
    rec["verification"]["exit_reason"] = "TURN_LIMIT"
    rec["termination"]["limit_hit"] = "max_steps"
    rec["cut_900s"]["exit_reason"] = "TURN_LIMIT"
    with pytest.raises(SchemaError, match="COMPLETED exactly"):
        validate_record(rec)
    rec = copy.deepcopy(line)
    rec["termination"]["cleanup"] = "ok"
    rec["verification"].update(patch_lines_added=None, patch_lines_removed=None)
    with pytest.raises(SchemaError, match="patch counts"):
        validate_record(rec)


def test_unknown_action_type_is_refused_even_without_arguments(line):
    rec = copy.deepcopy(line)
    rec["turns"][0].update(action_type="rm_rf", tool_args={})
    with pytest.raises(SchemaError, match="action_type"):
        validate_record(rec)


def test_turns_start_at_zero_without_a_resume(line):
    rec = copy.deepcopy(line)
    for t in rec["turns"]:
        t["turn_index"] += 2
    for cut, key in (("first_edit_cut", "cut_turn_index"), ("first_source_edit", "turn_index")):
        if rec[cut][key] is not None:
            rec[cut][key] += 2
    with pytest.raises(SchemaError, match="start at 0"):
        validate_record(rec)
