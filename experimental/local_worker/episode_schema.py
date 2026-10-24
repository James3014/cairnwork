"""The frozen schema of an episode corpus line (#88; EPISODE_CORPUS_SPEC_ZH §2-§6 + Plan v2).

``validate_record`` runs on every line before it is appended (corpus_io.append_episode): exact key
sets at every level, types with an explicit nullable flag (episode.NULL_RULE), the allowlisted tool
arguments, and the arithmetic (totals equal per-turn sums, total_turns equals len(turns)).
"""

from __future__ import annotations

from typing import Any

from experimental.local_worker.episode import (
    CUT_FIELDS,
    EXIT_REASONS,
    SCHEMA_VERSION,
    TEXT_ARGS,
    VERIFICATION_POST_STEP,
    token_totals,
)

STR, INT, FLOAT, BOOL, DICT, LIST = str, int, (int, float), bool, dict, list

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
}
CUT: dict[str, tuple[Any, bool]] = dict(
    zip(
        CUT_FIELDS,
        [
            (BOOL, False),
            (INT, True),
            (STR, True),
            (BOOL, True),
            (INT, True),
            (INT, True),
            (FLOAT, True),
            (STR, True),
            (BOOL, True),
            (LIST, True),
        ],
        strict=True,
    )
)
VERIFICATION: dict[str, tuple[Any, bool]] = {
    "exit_reason": (STR, False),
    "patch_lines_added": (INT, True),
    "patch_lines_removed": (INT, True),
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
    "tree_differs_from_base": (BOOL, True),
    "extra_tool_calls_ignored": (INT, False),
    "exec_identity": (STR, True),
    "isolation": (STR, True),
}
USAGE_STATES = ("ok", "no_cache_field", "malformed", "missing")


class SchemaError(ValueError):
    """A line that does not match the frozen schema is never appended."""


def _check(obj: Any, schema: dict[str, tuple[Any, bool]], where: str) -> None:
    if not isinstance(obj, dict):
        raise SchemaError(f"{where} is not an object")
    if set(obj) != set(schema):
        extra, missing = sorted(set(obj) - set(schema)), sorted(set(schema) - set(obj))
        raise SchemaError(f"{where}: unexpected keys {extra}, missing keys {missing}")
    for key, (typ, nullable) in schema.items():
        value = obj[key]
        if value is None:
            if not nullable:
                raise SchemaError(f"{where}.{key} must not be null")
            continue
        bad_bool = isinstance(value, bool) and typ is not BOOL
        if bad_bool or not isinstance(value, typ):
            raise SchemaError(f"{where}.{key} has type {type(value).__name__}")


def _check_tool_args(args: dict, where: str) -> None:
    for key, value in args.items():
        if key in TEXT_ARGS or (key == "path" and isinstance(value, dict)):
            if not (isinstance(value, dict) and set(value) == {"sha256", "bytes"}):
                raise SchemaError(f"{where}.{key} must be {{sha256, bytes}}")
        elif key == "path" or key == "status":
            if not isinstance(value, str):
                raise SchemaError(f"{where}.{key} must be a string")
        elif key == "target_index":
            if not isinstance(value, int) or isinstance(value, bool):
                raise SchemaError(f"{where}.{key} must be an integer")
        else:
            raise SchemaError(f"{where}: tool argument {key!r} is not allowlisted")


def _check_turns(turns: list) -> None:
    for n, turn in enumerate(turns):
        _check(turn, TURN, f"turns[{n}]")
        _check_tool_args(turn["tool_args"], f"turns[{n}].tool_args")
        if turn["usage_status"] not in USAGE_STATES:
            raise SchemaError(f"turns[{n}].usage_status {turn['usage_status']!r}")
        if turn["turn_index"] != turns[0]["turn_index"] + n:
            raise SchemaError(f"turns[{n}].turn_index is not consecutive")


def _check_cut(cut: dict) -> None:
    if not cut["cut_occurred"]:
        if any(cut[k] is not None for k in CUT_FIELDS[1:]):
            raise SchemaError("first_edit_cut: no cut, so every other field must be null")
        return
    for key in ("cut_turn_index", "pre_cut_wall_seconds", "cut_cause", "changed_paths"):
        if cut[key] is None:
            raise SchemaError(f"first_edit_cut.{key} must be set when a cut occurred")


def validate_record(rec: Any) -> None:
    """Raise SchemaError unless ``rec`` is a complete, consistent corpus line."""
    _check(rec, {**TOP_SPEC, **TOP_EXTRA}, "record")
    if rec["schema_version"] != SCHEMA_VERSION:
        raise SchemaError(f"schema_version {rec['schema_version']!r}")
    _check(rec["first_edit_cut"], CUT, "first_edit_cut")
    _check_cut(rec["first_edit_cut"])
    _check(rec["verification"], VERIFICATION, "verification")
    if rec["verification"]["exit_reason"] not in EXIT_REASONS:
        raise SchemaError(f"exit_reason {rec['verification']['exit_reason']!r}")
    _check(rec["spec_feedback"], SPEC_FEEDBACK, "spec_feedback")
    _check(rec["termination"], TERMINATION, "termination")
    _check(rec["cut_900s"], CUT_900S, "cut_900s")
    _check(rec["tokens_total"], TOKENS_TOTAL, "tokens_total")
    _check_turns(rec["turns"])
    if rec["total_turns"] != len(rec["turns"]):
        raise SchemaError("total_turns differs from len(turns)")
    if rec["tokens_total"] != token_totals(rec["turns"]):
        raise SchemaError("tokens_total differs from the per-turn sums")
