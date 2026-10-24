"""Schema checks of the two cuts of an episode line (#88; review findings 9-10, Codex B7).

Each cut object has cut_status CUT, NO_CUT or UNOBSERVED (its occurred flag True, False or null)
and must agree with the turns it was computed from: the edit cut is the first turn whose tool ran
and whose tree differs from base, unless a turn whose tool ran was never observed comes first
(UNOBSERVED); every pre-cut count is recomputed from the turns before the cut.
"""

from __future__ import annotations

from typing import Any

from experimental.local_worker.cuts import (
    CUT_FIELDS,
    CUT_PRE,
    CUT_STATES,
    READ_ACTIONS,
    SOURCE_FIELDS,
    SOURCE_PRE,
)
from experimental.local_worker.redact import MAX_CHANGED_PATHS, is_public_path
from experimental.local_worker.schema_base import (
    BOOL,
    FLOAT,
    INT,
    LIST,
    PATH,
    STR,
    SchemaError,
    check,
)

CUT: dict[str, tuple[Any, bool]] = dict(
    zip(
        CUT_FIELDS,
        [(BOOL, True), (INT, True), (PATH, True), (BOOL, True), (INT, True), (INT, True)]
        + [(FLOAT, True), (STR, True), (BOOL, True), (LIST, True), (INT, True), (INT, True)]
        + [(STR, False)],
        strict=True,
    )
)
SOURCE: dict[str, tuple[Any, bool]] = dict(
    zip(
        SOURCE_FIELDS,
        [(BOOL, True), (INT, True), (PATH, True), (STR, True), (INT, True), (INT, True)]
        + [(FLOAT, True), (INT, True), (STR, False)],
        strict=True,
    )
)
EDIT_REQUIRED = ("cut_turn_index", "pre_cut_wall_seconds", "cut_cause", "changed_paths")
EDIT_REQUIRED += ("changed_paths_total", "pre_cut_turns_missing_usage", "cut_ambiguous")
SOURCE_REQUIRED = ("turn_index", "path", "cause", "pre_wall_seconds", "pre_cut_turns_missing_usage")
_FLAG = {"CUT": True, "NO_CUT": False, "UNOBSERVED": None}


def pre_cut(turns: list, index: int) -> tuple[int, int | None, float, int]:
    """(read actions, token sum, wall seconds, turns missing usage) before turn ``index``,
    recomputed from the public turns exactly as cuts._pre computes them."""
    pos = index - turns[0]["turn_index"] if turns else -1
    if not 0 <= pos < len(turns):
        raise SchemaError(f"cut turn {index} is not among the turns")
    keys = ("tokens_prompt", "tokens_completion", "tokens_cached")
    before = turns[:pos]
    values = [t[k] for t in before for k in keys]
    tokens = None if any(v is None for v in values) else sum(values)
    missing = sum(any(t[k] is None for k in keys) for t in before)
    reads = sum(t["action_type"] in READ_ACTIONS for t in before)
    return reads, tokens, turns[pos]["t_start_seconds"], missing


def _one(cut: dict, fields: tuple[str, ...], spec: tuple, turns: list, where: str) -> None:
    """Status and flag agree; no cut: every other field null; a cut: the required fields set
    and the pre-cut values equal those recomputed from the turns."""
    index_key, pre_keys, required = spec
    status = cut["cut_status"]
    if status not in CUT_STATES or cut[fields[0]] is not _FLAG[status]:
        raise SchemaError(f"{where}: cut_status {status!r} with {fields[0]}={cut[fields[0]]!r}")
    if status != "CUT":
        if any(cut[k] is not None for k in fields[1:-1]):
            raise SchemaError(f"{where}: no cut, so every other field must be null")
        return
    for key in required:
        if cut[key] is None:
            raise SchemaError(f"{where}.{key} must be set when a cut occurred")
    if [cut[k] for k in pre_keys] != list(pre_cut(turns, cut[index_key])):
        raise SchemaError(f"{where}: pre-cut values differ from the turns before the cut")


def _paths(edit: dict, source: dict) -> None:
    """Review finding 9: every path is short or a digest; changed_paths keeps at most
    MAX_CHANGED_PATHS entries and changed_paths_total counts them all."""
    for value in (edit["target_file"], source["path"]):
        if value is not None and not is_public_path(value):
            raise SchemaError("a cut path must be a short path or {sha256, bytes}")
    paths, total = edit["changed_paths"], edit["changed_paths_total"]
    if paths is None:
        return
    if total is None or len(paths) != min(total, MAX_CHANGED_PATHS):
        raise SchemaError("changed_paths must hold min(changed_paths_total, MAX_CHANGED_PATHS)")
    if not all(is_public_path(v) for v in paths):
        raise SchemaError("changed_paths entries must be short paths or {sha256, bytes}")


def _expected_edit(turns: list) -> tuple[str, int | None]:
    for t in turns:
        if not t["tool_ran"]:
            if t["tree_differs_from_base"] is not None:
                raise SchemaError("a turn whose tool never ran has no tree observation")
            continue
        if t["tree_differs_from_base"] is None:
            return "UNOBSERVED", None
        if t["tree_differs_from_base"]:
            return "CUT", t["turn_index"]
    return "NO_CUT", None


def _source_against_turns(turns: list, edit: dict, source: dict) -> None:
    unobserved = [
        t["turn_index"] for t in turns if t["tool_ran"] and t["tree_differs_from_base"] is None
    ]
    status = source["cut_status"]
    if status == "CUT":
        turn = turns[source["turn_index"] - turns[0]["turn_index"]]
        ok = turn["tool_ran"] and turn["tree_differs_from_base"] is True
        after_edit = edit["cut_status"] == "CUT" and edit["cut_turn_index"] <= source["turn_index"]
        if not (ok and after_edit) or any(i < source["turn_index"] for i in unobserved):
            raise SchemaError("first_source_edit is not a tree change at or after the edit cut")
    elif status == "UNOBSERVED" and not unobserved:
        raise SchemaError("first_source_edit is UNOBSERVED but every tool's tree was observed")
    elif status == "NO_CUT" and (unobserved or edit["cut_status"] == "UNOBSERVED"):
        raise SchemaError("first_source_edit is NO_CUT although a tree was never observed")


def check_cuts(rec: dict) -> None:
    """Both cut objects of a line, against each other and against its (checked) turns."""
    turns, edit, source = rec["turns"], rec["first_edit_cut"], rec["first_source_edit"]
    check(edit, CUT, "first_edit_cut")
    check(source, SOURCE, "first_source_edit")
    _one(edit, CUT_FIELDS, ("cut_turn_index", CUT_PRE, EDIT_REQUIRED), turns, "first_edit_cut")
    spec = ("turn_index", SOURCE_PRE, SOURCE_REQUIRED)
    _one(source, SOURCE_FIELDS, spec, turns, "first_source_edit")
    _paths(edit, source)
    if (edit["cut_status"], edit["cut_turn_index"]) != _expected_edit(turns):
        raise SchemaError("first_edit_cut is not the first observed (or unobserved) tree change")
    _source_against_turns(turns, edit, source)
