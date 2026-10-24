"""Episode corpus record for the local worker loop (#88; EPISODE_CORPUS_SPEC_ZH §1-§6 + Plan v2).

Pure functions, no file I/O (corpus_io.py writes). One JSON line per episode, built from an
allowlist (redact.py): tool arguments keep a path only if it existed at base (any other path, and
one over 512 characters, becomes a digest with an allowlisted extension) and the test-target
index; written text (content, target, replacement) becomes ``{"sha256", "bytes"}``; tool outputs
keep only sizes, the truncation flag, the exit code and the error category. The task text, the
messages and every raw output stay in the private run directory. ``validate_record`` checks a
line against this frozen schema before it is appended.

Null versus zero (NULL_RULE): ``null`` means not observed, not applicable or not yet known (missing
``usage``, no cut, oracle fields before the post-step); ``0`` means observed and zero. A total is
null as soon as one of its parts is null; otherwise it equals the sum of the per-turn values.

The cut (Plan v2 point 4) comes from observed state: after every tool call the worker runs a fixed
``git status``; the cut is the first turn after which the tree differs from base (any path, tests
and docs included, side effects of run_tests included). Resuming from it is valid because every
turn before it left the tree at base.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from experimental.local_worker.cuts import (
    READ_ACTIONS,
    _sum,
    differs,
    first_edit_cut,
    first_source_edit,
    missing_usage,
)
from experimental.local_worker.gitstate import TreeState
from experimental.local_worker.redact import BasePaths, public_args, public_path

# v1 is the line shape of PR #248 (the #88 follow-up), which changed it (base_paths_observed,
# verification.binary_files_changed, path digests with ext) without a version bump: no v1 corpus
# was recorded before that change, so no v1 line has the PR #239 shape. Bump on the next change.
SCHEMA_VERSION = "cairnwork.local_worker.episode/v1"
NULL_RULE = "null = not observed / not applicable / not yet known; 0 = observed zero"
TOKENS_ACCUM_FORMULA = "prompt + completion + cached per call (spec §3), summed before the cut"
WALL_TIME_RULE = (  # recorded in manifest.json (Claude review of #239, NB9)
    "own_wall_seconds: this run alone (its turns' t_start/t_end, every cut field and cut_900s use"
    " this run's own clock). wall_clock_seconds = parent_pre_cut_wall_seconds (null, i.e. 0, for"
    " a fresh L0 run) + own_wall_seconds: the logical episode length the limits used. M5 time"
    " of a ladder = the L0 run's wall_clock_seconds + each resume run's own_wall_seconds; never"
    " add a resume run's wall_clock_seconds, which counts the parent's pre-cut time again."
)
EXIT_REASONS = ("COMPLETED", "TURN_LIMIT", "TIMEOUT", "ERROR_ABORT")


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
    tool_ran: bool = False  # a wrapper process of the tool started (it may have changed the tree)
    t_end: float = 0.0


def public_turn(t: TurnLog, base: TreeState, base_paths: BasePaths = None) -> dict:
    prompt, completion, cached = t.tokens
    return {
        "turn_index": t.index,
        "role": "assistant",
        "action_type": t.action_type,
        "tool_args": public_args(t.action_type, t.tool_args, t.exit_code, base_paths),
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
        "tool_ran": t.tool_ran,
        "tree_differs_from_base": differs(t.tree, base) if t.tool_ran else None,
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


def tree_at(turns: list[dict]) -> bool | None:
    """Whether the tree differed from base after these turns: the observation after the last
    turn whose tool ran (null if that one was never observed); False if no tool ran (base)."""
    ran = [t for t in turns if t["tool_ran"]]
    return ran[-1]["tree_differs_from_base"] if ran else False


def tree_at_limit(turns: list[dict], limit: float) -> bool | None:
    """The tree at ``limit`` seconds: tree_at of the turns ended by then; null when a turn whose
    tool ran spans the limit (started before it, ended after it), since when that tool changed
    the tree was never observed (Codex review of PR #248)."""
    if any(t["tool_ran"] and t["t_start_seconds"] < limit < t["t_end_seconds"] for t in turns):
        return None
    return tree_at([t for t in turns if t["t_end_seconds"] <= limit])


def cut_at(turns: list[dict], exit_reason: str, wall: float, limit: float) -> dict:
    """What the episode would have been with a ``limit``-second wall clock (addendum §0a item 4).
    turns_completed counts the turns ended by the limit; a turn spanning it is not completed."""
    return {
        "limit_seconds": limit,
        "within_limit": wall <= limit,
        "exit_reason": exit_reason if wall <= limit else "TIMEOUT",
        "turns_completed": sum(t["t_end_seconds"] <= limit for t in turns),
        "tree_differs_at_limit": tree_at_limit(turns, limit),
    }


@dataclass
class LoopOutcome:
    """What one run of the loop produced (agent.py fills it; build_record turns it into a line)."""

    base: TreeState
    base_paths: frozenset[str] | None = None  # the base commit's paths; None: not observed
    turns: list[TurnLog] = field(default_factory=list)
    exit_reason: str = "ERROR_ABORT"
    limit_hit: str | None = None
    timed_out: bool = False
    interrupted: bool = False
    abort_cause: str | None = None
    cleanup: str = "skipped"
    needs_spec: bool = False
    patch: tuple[int, int, int] | None = None  # added, removed, binary_files_changed
    started_at: str = ""
    ended_at: str = ""
    wall_seconds: float = 0.0  # this run's own seconds


def build_record(
    meta: dict,
    out: LoopOutcome,
    cut_seconds: float = 900.0,
    write_scope: tuple[str, ...] | None = None,
) -> dict:
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
    real timestamps. With a write scope (write_scope.py) the line also carries ``write_scope``:
    its entries, each through redact.public_path; without one the key is absent."""
    parent_wall = meta.get("parent_pre_cut_wall_seconds") or 0.0
    turns = [public_turn(t, out.base, out.base_paths) for t in out.turns]
    added, removed, binary = out.patch if out.patch is not None else (None, None, None)
    record = {
        "schema_version": SCHEMA_VERSION,
        **meta,
        "base_sha": out.base.head,
        "wall_clock_start": out.started_at,
        "wall_clock_end": out.ended_at,
        "wall_clock_seconds": round(parent_wall + out.wall_seconds, 3),
        "own_wall_seconds": round(out.wall_seconds, 3),
        "total_turns": len(turns),
        "first_edit_cut": first_edit_cut(out.turns, out.base, out.base_paths),
        "first_source_edit": first_source_edit(out.turns, out.base, out.base_paths),
        "base_paths_observed": out.base_paths is not None,
        "turns_missing_usage": sum(missing_usage(t) for t in out.turns),
        "turns": turns,
        "tokens_total": token_totals(turns),
        "usage_status": episode_usage_status(turns),
        "verification": {
            "exit_reason": out.exit_reason,
            "patch_lines_added": added,
            "patch_lines_removed": removed,
            "binary_files_changed": binary,
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
    if write_scope is not None:
        record["write_scope"] = [public_path(p, out.base_paths) for p in write_scope]
    return record


VERIFICATION_POST_STEP = (  # filled by the post-step (Cairnwork verify + the hidden-test oracle)
    "routine_passed",
    "public_tests_pass",
    "p2p_tests_pass",
    "scope_lock_pass",
    "ground_truth_pass",
    "verdict_category",
)
