"""Cross-field rules of an episode line (#88; Codex review of #239, non-blocking items).

Field types and key sets are checked in episode_schema; these rules check that the values agree:
closed vocabularies, turn times in order, the termination block against the exit reason, the
900-second cut against the turns, and the resume fields against each other.
"""

from __future__ import annotations

from experimental.local_worker.episode import tree_at
from experimental.local_worker.schema_base import SchemaError

ACTION_TYPES = ("list_dir", "read_file", "write_file", "edit_file", "run_test", "finish")
ACTION_TYPES += ("invalid_tool_call", "no_tool_call")
ERROR_CATEGORIES = ("NONE", "TIMEOUT", "REFUSED", "SYNTAX_ERROR", "TEST_FAILED", "NO_TESTS")
ERROR_CATEGORIES += ("TOOL_ERROR", "INVALID_CALL", "PROTOCOL_ERROR")
ABORT_CAUSES = ("endpoint_error", "worker_isolation", "status_unavailable", "protocol_error")
ABORT_CAUSES += ("internal_error", "interrupted")
LIMITS_HIT = ("max_steps", "max_wall_seconds", "context_cap", "context_cap_bytes_estimate")
CLEANUP = ("ok", "failed", "skipped")
EPISODE_USAGE = ("ok", "partial", "missing")
TOLERANCE = 0.002  # seconds: rounding to 3 decimals


def _vocabulary(rec: dict) -> None:
    term = rec["termination"]
    pairs = [
        (rec["usage_status"], EPISODE_USAGE, "usage_status"),
        (term["cleanup"], CLEANUP, "termination.cleanup"),
        (term["abort_cause"], (None, *ABORT_CAUSES), "termination.abort_cause"),
        (term["limit_hit"], (None, *LIMITS_HIT), "termination.limit_hit"),
    ]
    for n, t in enumerate(rec["turns"]):
        pairs.append((t["action_type"], ACTION_TYPES, f"turns[{n}].action_type"))
        pairs.append((t["error_category"], ERROR_CATEGORIES, f"turns[{n}].error_category"))
    for value, allowed, where in pairs:
        if value not in allowed:
            raise SchemaError(f"{where} {value!r} is not one of {allowed}")


def _times(rec: dict) -> None:
    previous = 0.0
    for n, t in enumerate(rec["turns"]):
        if not previous - TOLERANCE <= t["t_start_seconds"] <= t["t_end_seconds"] + TOLERANCE:
            raise SchemaError(f"turns[{n}] times are out of order")
        if t["t_end_seconds"] > rec["own_wall_seconds"] + TOLERANCE:
            raise SchemaError(f"turns[{n}] ends after the run ended")
        if t["exec_identity"] is not None and not t["tool_ran"]:
            raise SchemaError(f"turns[{n}] names who ran a tool that never ran")
        previous = t["t_end_seconds"]


def _termination(rec: dict) -> None:
    exit_reason, term = rec["verification"]["exit_reason"], rec["termination"]
    turns, added = rec["turns"], rec["verification"]["patch_lines_added"]
    if term["timed_out"] != (exit_reason == "TIMEOUT"):
        raise SchemaError("termination.timed_out must be true exactly for exit TIMEOUT")
    if (term["abort_cause"] is not None) != (exit_reason == "ERROR_ABORT"):
        raise SchemaError("termination.abort_cause is set exactly for exit ERROR_ABORT")
    if term["interrupted"] != (term["abort_cause"] == "interrupted"):
        raise SchemaError("termination.interrupted must match abort_cause interrupted")
    if (term["limit_hit"] is not None) != (exit_reason in ("TURN_LIMIT", "TIMEOUT")):
        raise SchemaError("termination.limit_hit is set exactly for TURN_LIMIT and TIMEOUT")
    finished = bool(turns) and turns[-1]["action_type"] == "finish"
    if (exit_reason == "COMPLETED") != finished:
        raise SchemaError("exit COMPLETED exactly when the last turn is finish")
    status = turns[-1]["tool_args"].get("status") if finished else None
    if rec["spec_feedback"]["needs_spec"] != (status == "NEEDS_SPEC"):
        raise SchemaError("spec_feedback.needs_spec must match finish(status=NEEDS_SPEC)")
    if (added is None) != (rec["verification"]["patch_lines_removed"] is None):
        raise SchemaError("patch_lines_added and patch_lines_removed are null together")
    if term["cleanup"] == "ok" and added is None:
        raise SchemaError("a cleanup that went ok has patch counts")


def _cut_900s(rec: dict) -> None:
    cut, own = rec["cut_900s"], rec["own_wall_seconds"]
    done = [t for t in rec["turns"] if t["t_end_seconds"] <= cut["limit_seconds"]]
    if cut["turns_completed"] != len(done) or cut["tree_differs_at_limit"] != tree_at(done):
        raise SchemaError("cut_900s differs from the turns within its limit")
    if abs(own - cut["limit_seconds"]) > TOLERANCE and cut["within_limit"] != (
        own <= cut["limit_seconds"]
    ):
        raise SchemaError("cut_900s.within_limit differs from own_wall_seconds")
    want = rec["verification"]["exit_reason"] if cut["within_limit"] else "TIMEOUT"
    if cut["exit_reason"] != want:
        raise SchemaError("cut_900s.exit_reason must be the exit reason, or TIMEOUT past it")


def _resume(rec: dict) -> None:
    parent, cut_turn = rec["resume_parent_episode_id"], rec["resume_cut_turn_index"]
    if (parent is None) != (cut_turn is None):
        raise SchemaError("resume_parent_episode_id and resume_cut_turn_index go together")
    first = rec["turns"][0]["turn_index"] if rec["turns"] else None
    if first is not None and first != (cut_turn or 0):
        raise SchemaError("turns start at 0, or at the resume cut turn")
    if parent is None and rec["plan_sha256"] is not None:
        raise SchemaError("a plan belongs to a resume run")


def check_rules(rec: dict) -> None:
    """Every cross-field rule of a line whose types and key sets were already checked."""
    _vocabulary(rec)
    _times(rec)
    _termination(rec)
    _cut_900s(rec)
    _resume(rec)
