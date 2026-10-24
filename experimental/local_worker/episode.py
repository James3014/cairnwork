"""Episode corpus record for the local worker loop (#88; EPISODE_CORPUS_SPEC_ZH §1-§6 + Plan v2).

Pure functions, no file I/O (corpus_io.py writes). One JSON line per episode, built from an
allowlist: tool arguments keep paths and the test-target index; written text (content, target,
replacement) becomes ``{"sha256", "bytes"}``; tool outputs keep only sizes, the truncation flag,
the exit code and the error category. The task text, the messages and every raw output stay in the
private run directory. ``validate_record`` checks a line against this frozen schema before it is
appended.

Null versus zero (NULL_RULE): ``null`` means not observed, not applicable or not yet known (missing
``usage``, no cut, oracle fields before the post-step); ``0`` means observed and zero. A total is
null as soon as one of its parts is null; otherwise it equals the sum of the per-turn values.

The cut (Plan v2 point 4) comes from observed state: after every tool call the worker runs a fixed
``git status``; the cut is the first turn after which the tree differs from base (any path, tests
and docs included, side effects of run_tests included). Resuming from it is valid because every
turn before it left the tree at base.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from experimental.local_worker.gitstate import TreeState

SCHEMA_VERSION = "cairnwork.local_worker.episode/v1"
NULL_RULE = "null = not observed / not applicable / not yet known; 0 = observed zero"
TOKENS_ACCUM_FORMULA = "prompt + completion + cached per call (spec §3), summed before the cut"
EXIT_REASONS = ("COMPLETED", "TURN_LIMIT", "TIMEOUT", "ERROR_ABORT")
READ_ACTIONS = ("read_file", "list_dir", "search")
EDIT_ACTIONS = ("write_file", "edit_file")
TEXT_ARGS = ("content", "target", "replacement")
MAX_PATH_CHARS = 512
DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
CUT_FIELDS = (
    "cut_occurred",
    "cut_turn_index",
    "target_file",
    "is_test_file",
    "pre_cut_read_actions",
    "pre_cut_tokens_accum",
    "pre_cut_wall_seconds",
    "cut_cause",
    "cut_ambiguous",
    "changed_paths",
    "pre_cut_turns_missing_usage",
)
SOURCE_FIELDS = (
    "occurred",
    "turn_index",
    "path",
    "cause",
    "pre_read_actions",
    "pre_tokens_accum",
    "pre_wall_seconds",
    "pre_cut_turns_missing_usage",
)
_TEST_NAME = re.compile(r"(^|[_.-])tests?([_.-]|$)")


@dataclass
class TurnLog:
    """Everything the loop knows about one turn; only ``public_turn`` of it reaches the corpus."""

    index: int
    t_start: float
    tokens: tuple[int | None, int | None, int | None] = (None, None, None)
    usage_status: str = "missing"
    usage_raw: Any = None
    server_model: str | None = None
    assistant_msg_index: int = -1
    action_type: str = "no_tool_call"
    tool_args: dict = field(default_factory=dict)
    exit_code: int | None = None
    stdout_bytes: int = 0
    stdout_truncated: bool = False
    error_category: str = "NONE"
    duration_ms: float = 0.0
    exec_identity: str | None = None
    isolation: str | None = None
    extra_tool_calls_ignored: int = 0
    tree: TreeState | None = None  # after a tool call; None when no tool ran
    t_end: float = 0.0


def is_test_path(path: str) -> bool:
    """In a tests/test/testing directory, conftest.py, or a name with a test/tests word part
    (test_x.py, x_test.py, tests.py; not attestation.py)."""
    parts = [p.lower() for p in path.split("/") if p]
    if not parts:
        return False
    name = parts[-1]
    in_test_dir = any(p in ("tests", "test", "testing") for p in parts[:-1])
    return in_test_dir or name == "conftest.py" or bool(_TEST_NAME.search(name.rsplit(".", 1)[0]))


def is_doc_path(path: str) -> bool:
    parts = [p.lower() for p in path.split("/") if p]
    if not parts:
        return False
    name = parts[-1]
    return (
        name.endswith(DOC_SUFFIXES)
        or any(p in ("docs", "doc") for p in parts[:-1])
        or name.startswith(("readme", "changelog", "license"))
    )


def differs(tree: TreeState | None, base: TreeState) -> bool | None:
    if tree is None or not tree.observed:
        return None
    return tree.head != base.head or tree.entries != base.entries


def _sum(values: list[int | None]) -> int | None:
    return None if any(v is None for v in values) else sum(v for v in values if v is not None)


def _cut_cause(turn: TurnLog, changed: list[str]) -> tuple[str, bool]:
    path = turn.tool_args.get("path")
    if turn.action_type in EDIT_ACTIONS and turn.exit_code == 0 and path in changed:
        return turn.action_type, changed != [path]
    if turn.action_type == "run_test":
        return "run_tests_side_effect", True
    return "other", True


def is_source_path(path: str) -> bool:
    """The spec's original edit target: neither a test file nor a doc file."""
    return not is_test_path(path) and not is_doc_path(path)


def missing_usage(t: TurnLog) -> bool:
    """A turn whose token triple is incomplete (no, malformed or cache-less usage): any sum over
    it is null, and this counter says how many such turns made it null."""
    return any(v is None for v in t.tokens)


def _first_change(turns: list[TurnLog], base: TreeState, source_only: bool):
    """(position, turn, changed paths) of the first turn whose observed tree differs from base
    (in a source path, when ``source_only``), else None."""
    for n, turn in enumerate(turns):
        if not differs(turn.tree, base):
            continue
        assert turn.tree is not None
        changed = [p for p in turn.tree.paths() if p not in base.paths()] or turn.tree.paths()
        if source_only:  # git-ignored paths (status "!") are artifacts, never source
            ignored = {e[2:] for e in turn.tree.entries if e.startswith("! ")}
            changed = [p for p in changed if is_source_path(p) and p not in ignored]
        if changed or not source_only:
            return n, turn, changed
    return None


def _pre(before: list[TurnLog], turn: TurnLog) -> tuple[int, int | None, float, int]:
    reads = sum(t.action_type in READ_ACTIONS for t in before)
    tokens = _sum([v for t in before for v in t.tokens])
    return reads, tokens, round(turn.t_start, 3), sum(missing_usage(t) for t in before)


def first_edit_cut(turns: list[TurnLog], base: TreeState) -> dict:
    """The spec §3 object, cut where the tree first differs from base (any path: what resume
    needs), plus cut_cause, cut_ambiguous, changed_paths and pre_cut_turns_missing_usage."""
    found = _first_change(turns, base, source_only=False)
    if found is None:
        return {"cut_occurred": False, **dict.fromkeys(CUT_FIELDS[1:])}
    n, turn, changed = found
    cause, ambiguous = _cut_cause(turn, changed)
    target = turn.tool_args.get("path") if cause in EDIT_ACTIONS else (changed or [None])[0]
    reads, tokens, wall, missing = _pre(turns[:n], turn)
    return {
        "cut_occurred": True,
        "cut_turn_index": turn.index,
        "target_file": target,
        "is_test_file": is_test_path(target) if isinstance(target, str) else None,
        "pre_cut_read_actions": reads,
        "pre_cut_tokens_accum": tokens,
        "pre_cut_wall_seconds": wall,
        "cut_cause": cause,
        "cut_ambiguous": ambiguous,
        "changed_paths": changed,
        "pre_cut_turns_missing_usage": missing,
    }


def first_source_edit(turns: list[TurnLog], base: TreeState) -> dict:
    """The spec's original cut: the first turn whose observed changed paths include a source
    file (is_source_path, and not git-ignored). It equals first_edit_cut unless a test, doc or
    run_tests artifact changed the tree first; resume always uses first_edit_cut."""
    found = _first_change(turns, base, source_only=True)
    if found is None:
        return {"occurred": False, **dict.fromkeys(SOURCE_FIELDS[1:])}
    n, turn, changed = found
    cause, _ = _cut_cause(turn, changed)
    path = turn.tool_args.get("path") if cause in EDIT_ACTIONS else changed[0]
    reads, tokens, wall, missing = _pre(turns[:n], turn)
    return dict(
        zip(
            SOURCE_FIELDS,
            (True, turn.index, path, cause, reads, tokens, wall, missing),
            strict=True,
        )
    )


def _digest(text: Any) -> dict:
    data = str(text).encode("utf-8", errors="surrogatepass")
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def public_args(args: dict) -> dict:
    """Allowlisted tool arguments: path, target_index; written text only as sha256 and size."""
    out: dict[str, Any] = {}
    path = args.get("path")
    if isinstance(path, str):
        out["path"] = path if len(path) <= MAX_PATH_CHARS else _digest(path)
    index = args.get("target_index")
    if isinstance(index, int) and not isinstance(index, bool):
        out["target_index"] = index
    for key in TEXT_ARGS:
        if key in args:
            out[key] = _digest(args[key])
    if "status" in args and args["status"] in ("DONE", "NEEDS_SPEC"):
        out["status"] = args["status"]
    return out


def public_turn(t: TurnLog, base: TreeState) -> dict:
    prompt, completion, cached = t.tokens
    return {
        "turn_index": t.index,
        "role": "assistant",
        "action_type": t.action_type,
        "tool_args": public_args(t.tool_args),
        "exit_code": t.exit_code,
        "stdout_bytes": t.stdout_bytes,
        "stdout_truncated": t.stdout_truncated,
        "error_category": t.error_category,
        "duration_ms": t.duration_ms,
        "tokens_prompt": prompt,
        "tokens_completion": completion,
        "tokens_cached": cached,
        "usage_status": t.usage_status,
        "t_start_seconds": round(t.t_start, 3),
        "t_end_seconds": round(t.t_end, 3),
        "tree_differs_from_base": differs(t.tree, base),
        "extra_tool_calls_ignored": t.extra_tool_calls_ignored,
        "exec_identity": t.exec_identity,
        "isolation": t.isolation,
    }


PARENT_FIELDS = (
    "parent_pre_cut_read_actions",
    "parent_pre_cut_tokens_accum",
    "parent_pre_cut_wall_seconds",
    "parent_pre_cut_turns_missing_usage",
)


def parent_prefix(rows: list[dict], cut_turn: int) -> dict:
    """The parent_* fields of a resume run, from the parent transcript's turn rows before
    ``cut_turn`` (the parent is a fresh run, so its clock is its own)."""
    before = [r for r in rows if r["index"] < cut_turn]
    start = next(r["t_start"] for r in rows if r["index"] == cut_turn)
    tokens = [v for r in before for v in r["tokens"]]
    return dict(
        zip(
            PARENT_FIELDS,
            (
                sum(r["action_type"] in READ_ACTIONS for r in before),
                _sum(tokens),
                round(float(start), 3),
                sum(any(v is None for v in r["tokens"]) for r in before),
            ),
            strict=True,
        )
    )


def token_totals(turns: list[dict]) -> dict:
    return {k: _sum([t[f"tokens_{k}"] for t in turns]) for k in ("prompt", "completion", "cached")}


def episode_usage_status(turns: list[dict]) -> str:
    states = {t["usage_status"] for t in turns}
    if not states or states == {"ok"}:
        return "ok"
    return "missing" if states == {"missing"} else "partial"


def cut_at(turns: list[dict], exit_reason: str, wall: float, limit: float) -> dict:
    """What the episode would have been with a ``limit``-second wall clock (addendum §0a item 4)."""
    done = [t for t in turns if t["t_end_seconds"] <= limit]
    seen = [t["tree_differs_from_base"] for t in done if t["tree_differs_from_base"] is not None]
    return {
        "limit_seconds": limit,
        "within_limit": wall <= limit,
        "exit_reason": exit_reason if wall <= limit else "TIMEOUT",
        "turns_completed": len(done),
        "tree_differs_at_limit": seen[-1] if seen else None,
    }


@dataclass
class LoopOutcome:
    """What one run of the loop produced (agent.py fills it; build_record turns it into a line)."""

    base: TreeState
    turns: list[TurnLog] = field(default_factory=list)
    exit_reason: str = "ERROR_ABORT"
    limit_hit: str | None = None
    timed_out: bool = False
    interrupted: bool = False
    abort_cause: str | None = None
    cleanup: str = "skipped"
    needs_spec: bool = False
    patch: tuple[int, int] | None = None
    started_at: str = ""
    ended_at: str = ""
    wall_seconds: float = 0.0  # this run's own seconds


def build_record(meta: dict, out: LoopOutcome, cut_seconds: float = 900.0) -> dict:
    """The corpus line. ``meta`` carries the identity fields (episode_id, task_id, repo,
    model_name, harness_version, arm, seed, endpoint, server_model_id, manifest_sha256,
    canary_report_sha256, checkout_id and the resume_* / plan_sha256 fields).

    One basis, this run's own (review of #239, finding 1): turns' t_start/t_end, every cut
    field (pre_cut_* counts, tokens and seconds cover only this run's turns) and
    ``own_wall_seconds`` describe this run alone. A resume run carries its parent's prefix only
    in the explicit ``parent_pre_cut_*`` fields (null for a fresh run), and the one field that
    adds them up is ``wall_clock_seconds`` = parent_pre_cut_wall_seconds + own_wall_seconds,
    the logical episode length its limits used. cut_900s asks what would have happened with the
    900-second episode budget: on this run's own clock its limit is 900 minus the parent's
    pre-cut seconds. To total M5 time for L0 plus its resume arms, sum L0's
    ``wall_clock_seconds`` and each resume run's ``own_wall_seconds``; summing the resume's
    wall_clock_seconds would count the pre-cut time twice. wall_clock_start/end are this run's
    real timestamps."""
    parent_wall = meta.get("parent_pre_cut_wall_seconds") or 0.0
    turns = [public_turn(t, out.base) for t in out.turns]
    added, removed = out.patch if out.patch is not None else (None, None)
    record = {
        "schema_version": SCHEMA_VERSION,
        **meta,
        "base_sha": out.base.head,
        "wall_clock_start": out.started_at,
        "wall_clock_end": out.ended_at,
        "wall_clock_seconds": round(parent_wall + out.wall_seconds, 3),
        "own_wall_seconds": round(out.wall_seconds, 3),
        "total_turns": len(turns),
        "first_edit_cut": first_edit_cut(out.turns, out.base),
        "first_source_edit": first_source_edit(out.turns, out.base),
        "turns_missing_usage": sum(missing_usage(t) for t in out.turns),
        "turns": turns,
        "tokens_total": token_totals(turns),
        "usage_status": episode_usage_status(turns),
        "verification": {
            "exit_reason": out.exit_reason,
            "patch_lines_added": added,
            "patch_lines_removed": removed,
            **dict.fromkeys(VERIFICATION_POST_STEP),
        },
        "spec_feedback": {
            "needs_spec": out.needs_spec,
            "missing_fact_type": None,
            "extracted_evidence": None,
            "oracle_resolvable": None,
        },
        "termination": {
            "limit_hit": out.limit_hit,
            "timed_out": out.timed_out,
            "interrupted": out.interrupted,
            "abort_cause": out.abort_cause,
            "cleanup": out.cleanup,
        },
        "cut_900s": cut_at(
            turns, out.exit_reason, out.wall_seconds, max(0.0, cut_seconds - parent_wall)
        ),
    }
    return record


VERIFICATION_POST_STEP = (  # filled by the post-step (Cairnwork verify + the hidden-test oracle)
    "routine_passed",
    "public_tests_pass",
    "p2p_tests_pass",
    "scope_lock_pass",
    "ground_truth_pass",
    "verdict_category",
)
