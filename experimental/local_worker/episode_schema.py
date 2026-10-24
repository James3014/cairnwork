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
    PARENT_FIELDS,
    READ_ACTIONS,
    SCHEMA_VERSION,
    SOURCE_FIELDS,
    VERIFICATION_POST_STEP,
    token_totals,
)
from experimental.local_worker.redact import MAX_CHANGED_PATHS, TEXT_ARGS, is_digest, is_public_path

STR, INT, FLOAT, BOOL, DICT, LIST = str, int, (int, float), bool, dict, list
PATH = (str, dict)  # a path, or {sha256, bytes} when longer than redact.MAX_PATH_CHARS

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
CUT: dict[str, tuple[Any, bool]] = dict(
    zip(
        CUT_FIELDS,
        [
            (BOOL, False),
            (INT, True),
            (PATH, True),
            (BOOL, True),
            (INT, True),
            (INT, True),
            (FLOAT, True),
            (STR, True),
            (BOOL, True),
            (LIST, True),
            (INT, True),
            (INT, True),
        ],
        strict=True,
    )
)
SOURCE: dict[str, tuple[Any, bool]] = dict(
    zip(
        SOURCE_FIELDS,
        [(BOOL, False), (INT, True), (PATH, True), (STR, True), (INT, True), (INT, True)]
        + [(FLOAT, True), (INT, True)],
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
        if key in TEXT_ARGS:
            if not is_digest(value):
                raise SchemaError(f"{where}.{key} must be {{sha256, bytes}}")
        elif key == "path":
            if not is_public_path(value):
                raise SchemaError(f"{where}.path must be a short path or {{sha256, bytes}}")
        elif key == "status":
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


def _check_cut(cut: dict, fields: tuple[str, ...], required: tuple[str, ...], where: str) -> None:
    """No cut: every other field null. A cut: the required fields set, and the token sum null
    exactly when some turn before the cut lacked usage (pre_cut_turns_missing_usage > 0)."""
    if not cut[fields[0]]:
        if any(cut[k] is not None for k in fields[1:]):
            raise SchemaError(f"{where}: no cut, so every other field must be null")
        return
    for key in required:
        if cut[key] is None:
            raise SchemaError(f"{where}.{key} must be set when a cut occurred")
    accum = cut["pre_cut_tokens_accum" if "pre_cut_tokens_accum" in cut else "pre_tokens_accum"]
    if (accum is None) != (cut["pre_cut_turns_missing_usage"] > 0):
        raise SchemaError(f"{where}: token sum null exactly when a pre-cut turn lacks usage")


def _check_cuts(rec: dict) -> None:
    edit, source = rec["first_edit_cut"], rec["first_source_edit"]
    _check(edit, CUT, "first_edit_cut")
    _check(source, SOURCE, "first_source_edit")
    required = ("cut_turn_index", "pre_cut_wall_seconds", "cut_cause", "changed_paths")
    required += ("changed_paths_total",)
    _check_cut(edit, CUT_FIELDS, (*required, "pre_cut_turns_missing_usage"), "first_edit_cut")
    required = ("turn_index", "path", "cause", "pre_wall_seconds", "pre_cut_turns_missing_usage")
    _check_cut(source, SOURCE_FIELDS, required, "first_source_edit")
    _check_paths(edit, source)
    if source["occurred"] and not (
        edit["cut_occurred"] and edit["cut_turn_index"] <= source["turn_index"]
    ):
        raise SchemaError("first_source_edit precedes the first tree change")


def _check_paths(edit: dict, source: dict) -> None:
    """Review finding 9: every path is short or a digest; changed_paths keeps at most
    MAX_CHANGED_PATHS entries and changed_paths_total counts them all."""
    for value in (edit["target_file"], source["path"]):
        if value is not None and not is_public_path(value):
            raise SchemaError("a cut path must be a short path or {sha256, bytes}")
    paths, total = edit["changed_paths"], edit["changed_paths_total"]
    if paths is None:
        if total is not None:
            raise SchemaError("changed_paths_total without changed_paths")
        return
    if total is None or len(paths) != min(total, MAX_CHANGED_PATHS):
        raise SchemaError("changed_paths must hold min(changed_paths_total, MAX_CHANGED_PATHS)")
    if not all(is_public_path(v) for v in paths):
        raise SchemaError("changed_paths entries must be short paths or {sha256, bytes}")


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


def _pre_cut(turns: list, index: int) -> tuple[int, int | None, float, int]:
    """(read actions, token sum, wall seconds, turns missing usage) before turn ``index``,
    recomputed from the public turns exactly as episode._pre computes them."""
    pos = index - turns[0]["turn_index"]
    if not 0 <= pos < len(turns):
        raise SchemaError(f"cut turn {index} is not among the turns")
    keys = ("tokens_prompt", "tokens_completion", "tokens_cached")
    before = turns[:pos]
    values = [t[k] for t in before for k in keys]
    tokens = None if any(v is None for v in values) else sum(values)
    missing = sum(any(t[k] is None for k in keys) for t in before)
    reads = sum(t["action_type"] in READ_ACTIONS for t in before)
    return reads, tokens, turns[pos]["t_start_seconds"], missing


def _check_pre_cut(rec: dict) -> None:
    """Review finding 10: each cut's pre-cut counts equal those recomputed from the turns, and
    the edit cut is the first turn whose observed tree differs from base."""
    turns, edit, source = rec["turns"], rec["first_edit_cut"], rec["first_source_edit"]
    differs = [n for n, t in enumerate(turns) if t["tree_differs_from_base"] is True]
    if edit["cut_occurred"]:
        if not differs or turns[differs[0]]["turn_index"] != edit["cut_turn_index"]:
            raise SchemaError("first_edit_cut is not the first turn whose tree differs from base")
        got = [edit[k] for k in CUT_FIELDS if k.startswith("pre_cut_")]
        if got != list(_pre_cut(turns, edit["cut_turn_index"])):
            raise SchemaError("first_edit_cut pre-cut values differ from the turns before it")
    elif differs:
        raise SchemaError("a turn's tree differs from base but first_edit_cut did not occur")
    if source["occurred"]:
        got = [source[k] for k in SOURCE_FIELDS[4:]]
        if got != list(_pre_cut(turns, source["turn_index"])):
            raise SchemaError("first_source_edit pre values differ from the turns before it")


def validate_record(rec: Any) -> None:
    """Raise SchemaError unless ``rec`` is a complete, consistent corpus line."""
    _check(rec, {**TOP_SPEC, **TOP_EXTRA}, "record")
    if rec["schema_version"] != SCHEMA_VERSION:
        raise SchemaError(f"schema_version {rec['schema_version']!r}")
    _check_cuts(rec)
    _check(rec["verification"], VERIFICATION, "verification")
    if rec["verification"]["exit_reason"] not in EXIT_REASONS:
        raise SchemaError(f"exit_reason {rec['verification']['exit_reason']!r}")
    _check(rec["spec_feedback"], SPEC_FEEDBACK, "spec_feedback")
    _check(rec["termination"], TERMINATION, "termination")
    _check(rec["cut_900s"], CUT_900S, "cut_900s")
    _check(rec["tokens_total"], TOKENS_TOTAL, "tokens_total")
    _check_turns(rec["turns"])
    _check_counts(rec)
    _check_pre_cut(rec)
