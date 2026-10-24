"""The frozen schema of an episode corpus line (#88; EPISODE_CORPUS_SPEC_ZH §2-§6 + Plan v2).

``validate_record`` runs on every line before it is appended (corpus_io.append_episode): exact key
sets at every level, types with an explicit nullable flag (episode.NULL_RULE), the allowlisted tool
arguments, and the arithmetic (totals equal per-turn sums, total_turns equals len(turns)).
"""

from __future__ import annotations

from typing import Any

from experimental.local_worker.episode import (
    EXIT_REASONS,
    PARENT_FIELDS,
    SCHEMA_VERSION,
    VERIFICATION_POST_STEP,
    token_totals,
)
from experimental.local_worker.redact import (
    PUBLIC_ARGS,
    TEXT_ARGS,
    is_digest,
    is_public_path,
)
from experimental.local_worker.schema_base import (
    BOOL,
    DICT,
    FLOAT,
    INT,
    LIST,
    STR,
    SchemaError,
    check,
)
from experimental.local_worker.schema_cuts import check_cuts
from experimental.local_worker.schema_rules import check_rules

TOP_SPEC: dict[str, tuple[Any, bool]] = {  # spec §2: (type, nullable)
    "episode_id": (STR, False),
    "task_id": (STR, False),
    "repo": (STR, False),
    "model_name": (STR, False),
    "harness_version": (STR, False),
    "wall_clock_start": (STR, False),
    "wall_clock_end": (STR, False),
    "wall_clock_seconds": (FLOAT, False),
    "total_turns": (INT, False),
    "first_edit_cut": (DICT, False),
    "turns": (LIST, False),
    "verification": (DICT, False),
    "spec_feedback": (DICT, False),
}
TOP_EXTRA: dict[str, tuple[Any, bool]] = {  # Plan v2 point 10 and the bindings
    "schema_version": (STR, False),
    "base_sha": (STR, False),
    "checkout_id": (STR, False),
    "arm": (STR, False),
    "seed": (INT, True),
    "resume_parent_episode_id": (STR, True),
    "resume_cut_turn_index": (INT, True),
    "plan_sha256": (STR, True),
    "endpoint": (STR, False),
    "server_model_id": (STR, True),
    "manifest_sha256": (STR, False),
    "canary_report_sha256": (STR, False),
    "tokens_total": (DICT, False),
    "usage_status": (STR, False),
    "termination": (DICT, False),
    "cut_900s": (DICT, False),
    "own_wall_seconds": (FLOAT, False),
    "first_source_edit": (DICT, False),
    "turns_missing_usage": (INT, False),
    "parent_pre_cut_read_actions": (INT, True),
    "parent_pre_cut_tokens_accum": (INT, True),
    "parent_pre_cut_wall_seconds": (FLOAT, True),
    "parent_pre_cut_turns_missing_usage": (INT, True),
}
VERIFICATION: dict[str, tuple[Any, bool]] = {
    "exit_reason": (STR, False),
    "patch_lines_added": (INT, True),
    "patch_lines_removed": (INT, True),
    "binary_files_changed": (INT, True),  # files git's numstat takes for binary (still counted)
    **{k: ((STR if k == "verdict_category" else BOOL), True) for k in VERIFICATION_POST_STEP},
}
SPEC_FEEDBACK: dict[str, tuple[Any, bool]] = {
    "needs_spec": (BOOL, False),
    "missing_fact_type": (STR, True),
    "extracted_evidence": (STR, True),
    "oracle_resolvable": (BOOL, True),
}
TERMINATION: dict[str, tuple[Any, bool]] = {
    "limit_hit": (STR, True),
    "timed_out": (BOOL, False),
    "interrupted": (BOOL, False),
    "abort_cause": (STR, True),
    "cleanup": (STR, False),
}
CUT_900S: dict[str, tuple[Any, bool]] = {
    "limit_seconds": (FLOAT, False),
    "within_limit": (BOOL, False),
    "exit_reason": (STR, False),
    "turns_completed": (INT, False),
    "tree_differs_at_limit": (BOOL, True),
}
TOKENS_TOTAL: dict[str, tuple[Any, bool]] = {
    k: (INT, True) for k in ("prompt", "completion", "cached")
}
TURN: dict[str, tuple[Any, bool]] = {
    "turn_index": (INT, False),
    "role": (STR, False),
    "action_type": (STR, False),
    "tool_args": (DICT, False),
    "exit_code": (INT, True),
    "stdout_bytes": (INT, False),
    "stdout_truncated": (BOOL, False),
    "error_category": (STR, False),
    "duration_ms": (FLOAT, False),
    "tokens_prompt": (INT, True),
    "tokens_completion": (INT, True),
    "tokens_cached": (INT, True),
    "usage_status": (STR, False),
    "t_start_seconds": (FLOAT, False),
    "t_end_seconds": (FLOAT, False),
    "tool_ran": (BOOL, False),
    "tree_differs_from_base": (BOOL, True),
    "extra_tool_calls_ignored": (INT, False),
    "exec_identity": (STR, True),
    "isolation": (STR, True),
}
USAGE_STATES = ("ok", "no_cache_field", "malformed", "missing")


def _check_tool_args(turn: dict, where: str) -> None:
    """Only the action's PUBLIC_ARGS keys; text as a digest; a readable path only after exit 0."""
    args, allowed = turn["tool_args"], PUBLIC_ARGS.get(turn["action_type"], ())
    for key, value in args.items():
        if key not in allowed:
            raise SchemaError(f"{where}: {key!r} is not a public argument of this action")
        if key in TEXT_ARGS:
            if not is_digest(value):
                raise SchemaError(f"{where}.{key} must be {{sha256, bytes}}")
        elif key == "path":
            plain_ok = turn["exit_code"] == 0 and is_public_path(value)
            if not (is_digest(value) or plain_ok):
                raise SchemaError(f"{where}.path is readable only after a successful call")
        elif key == "status":
            if value not in ("DONE", "NEEDS_SPEC"):
                raise SchemaError(f"{where}.status {value!r}")
        elif not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise SchemaError(f"{where}.{key} must be a non-negative integer")


def _check_turns(turns: list) -> None:
    for n, turn in enumerate(turns):
        check(turn, TURN, f"turns[{n}]")
        _check_tool_args(turn, f"turns[{n}].tool_args")
        if turn["usage_status"] not in USAGE_STATES:
            raise SchemaError(f"turns[{n}].usage_status {turn['usage_status']!r}")
        if turn["turn_index"] != turns[0]["turn_index"] + n:
            raise SchemaError(f"turns[{n}].turn_index is not consecutive")


def _check_counts(rec: dict) -> None:
    turns = rec["turns"]
    if rec["total_turns"] != len(turns):
        raise SchemaError("total_turns differs from len(turns)")
    if rec["tokens_total"] != token_totals(turns):
        raise SchemaError("tokens_total differs from the per-turn sums")
    keys = ("tokens_prompt", "tokens_completion", "tokens_cached")
    missing = sum(any(t[k] is None for k in keys) for t in turns)
    if rec["turns_missing_usage"] != missing:
        raise SchemaError("turns_missing_usage differs from the turns without full usage")
    _check_parent(rec)


def _check_parent(rec: dict) -> None:
    """Fresh run: no parent_* field. Resume run: all set (the token sum null exactly when a
    prefix turn lacked usage), and wall_clock_seconds = parent pre-cut + own seconds."""
    parent = [rec[k] for k in PARENT_FIELDS]
    if rec["resume_parent_episode_id"] is None:
        if any(v is not None for v in parent):
            raise SchemaError("a fresh run has no parent_pre_cut_* values")
    else:
        reads, tokens, wall, missing = parent
        if reads is None or wall is None or missing is None or (tokens is None) != (missing > 0):
            raise SchemaError("a resume run needs consistent parent_pre_cut_* values")
    total = (rec["parent_pre_cut_wall_seconds"] or 0.0) + rec["own_wall_seconds"]
    if abs(rec["wall_clock_seconds"] - total) > 0.002:
        raise SchemaError("wall_clock_seconds must be parent pre-cut seconds + own seconds")


def validate_record(rec: Any) -> None:
    """Raise SchemaError unless ``rec`` is a complete, consistent corpus line."""
    check(rec, {**TOP_SPEC, **TOP_EXTRA}, "record")
    if rec["schema_version"] != SCHEMA_VERSION:
        raise SchemaError(f"schema_version {rec['schema_version']!r}")
    check(rec["verification"], VERIFICATION, "verification")
    if rec["verification"]["exit_reason"] not in EXIT_REASONS:
        raise SchemaError(f"exit_reason {rec['verification']['exit_reason']!r}")
    check(rec["spec_feedback"], SPEC_FEEDBACK, "spec_feedback")
    check(rec["termination"], TERMINATION, "termination")
    check(rec["cut_900s"], CUT_900S, "cut_900s")
    check(rec["tokens_total"], TOKENS_TOTAL, "tokens_total")
    _check_turns(rec["turns"])
    _check_counts(rec)
    check_cuts(rec)
    check_rules(rec)
