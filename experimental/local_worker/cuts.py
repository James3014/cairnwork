"""The cuts of an episode (#88): where the observed tree first left base (Plan v2 point 4).

After every tool call the worker runs a fixed ``git status``; ``first_edit_cut`` is the first turn
after which that tree differs from base (any path), ``first_source_edit`` the first whose changed
paths include a source file (the spec's original cut). A turn whose tool started but whose tree
was never observed (the status query failed, or the run stopped before it) makes the cut
indeterminate from there on: cut_status "UNOBSERVED" with cut_occurred / occurred null, never
"no edit" (Codex review of #239, B7). Such a cut is never evidence of a clean prefix: resume
refuses it.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from experimental.local_worker.gitstate import TreeState
from experimental.local_worker.redact import BasePaths, public_path, public_paths

if TYPE_CHECKING:
    from experimental.local_worker.episode import TurnLog

CUT_DEFINITION = (  # recorded in manifest.json (review of #239, finding 10)
    "first_edit_cut: the first turn after which the worker-observed git status (porcelain v2,"
    " -uall, --ignored=matching) differs from base in any path; test files, doc files and"
    " run_tests artifacts count. This deviates from EPISODE_CORPUS_SPEC_ZH §3/§8 (first edit of"
    " a source file) per Plan v2 point 4, so that resume from it is valid. first_source_edit"
    " carries the spec's cut: the first observed change to a path that is not a test, doc or"
    " git-ignored file; a new or changed .gitignore is itself a source change even when it"
    " ignores itself (so one planted to hide new source files sets the cut). cut_status"
    " UNOBSERVED (cut_occurred null): a turn whose tool ran was never observed before any cut,"
    " so whether and where the tree changed is unknown."
    " Besides git's status, the observation lists the empty directories, FIFOs and sockets in"
    " the checkout (git cannot see them), so creating one is a tree change for first_edit_cut;"
    " they are never a source change. Not observed: anything outside the checkout (run_root/tmp,"
    " the test process's HOME and TMPDIR), file modes git does not track, and the contents of"
    " a FIFO."
)
CUT_STATES = ("CUT", "NO_CUT", "UNOBSERVED")
READ_ACTIONS = ("read_file", "list_dir")  # the read tools the loop offers (no search tool)
EDIT_ACTIONS = ("write_file", "edit_file")
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
    "changed_paths_total",
    "pre_cut_turns_missing_usage",
    "cut_status",
)
CUT_PRE = (
    "pre_cut_read_actions",
    "pre_cut_tokens_accum",
    "pre_cut_wall_seconds",
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
    "cut_status",
)
SOURCE_PRE = (
    "pre_read_actions",
    "pre_tokens_accum",
    "pre_wall_seconds",
    "pre_cut_turns_missing_usage",
)
_TEST_NAME = re.compile(r"(^|[_.-])tests?([_.-]|$)")


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


def is_ignore_file(path: str) -> bool:
    """A .gitignore (any case: git opens it by name on a case-insensitive file system)."""
    return path.rsplit("/", 1)[-1].lower() == ".gitignore"


def _artifacts(tree: TreeState) -> set[str]:
    """Paths of a tree that are never a source edit: git-ignored ones (status "!"), except an
    ignore file, and what git cannot see (x: an empty directory, a FIFO or a socket).

    Claude review of #239, NB3: a planted ``src/.gitignore`` containing ``*`` ignores itself
    and every new file beside it, so ignored-means-artifact would hide new source files. Only
    the base's ignore rules may make a path an artifact; the observation cannot evaluate those
    rules alone, so a new or changed ignore file is instead always a source change: the cut then
    falls at the turn that planted it (or earlier, at a source file it later hides), and no
    rule a worker wrote can move first_source_edit later."""
    ignored = {e[2:] for e in tree.entries if e.startswith("! ") and not is_ignore_file(e[2:])}
    return ignored | {e[4:] for e in tree.entries if e.startswith("x ")}


def _first_change(turns: list[TurnLog], base: TreeState, source_only: bool):
    """(status, position, turn, changed paths): "CUT" at the first turn whose observed tree
    differs from base (in a source path, when ``source_only``); "UNOBSERVED" at the first turn
    before that whose tool ran but whose tree was never observed; else "NO_CUT"."""
    for n, turn in enumerate(turns):
        if not turn.tool_ran:
            continue  # nothing ran: the tree is what the last observation saw
        changed_tree = differs(turn.tree, base)
        if changed_tree is None:
            return "UNOBSERVED", n, turn, []
        if not changed_tree:
            continue
        assert turn.tree is not None
        changed = [p for p in turn.tree.paths() if p not in base.paths()] or turn.tree.paths()
        if source_only:
            changed = [p for p in changed if is_source_path(p) and p not in _artifacts(turn.tree)]
        if changed or not source_only:
            return "CUT", n, turn, changed
    return "NO_CUT", None, None, []


def _no_cut(fields: tuple[str, ...], status: str) -> dict:
    occurred = False if status == "NO_CUT" else None
    return {fields[0]: occurred, **dict.fromkeys(fields[1:-1]), "cut_status": status}


def _pre(before: list[TurnLog], turn: TurnLog) -> tuple[int, int | None, float, int]:
    reads = sum(t.action_type in READ_ACTIONS for t in before)
    tokens = _sum([v for t in before for v in t.tokens])
    return reads, tokens, round(turn.t_start, 3), sum(missing_usage(t) for t in before)


def first_edit_cut(turns: list[TurnLog], base: TreeState, base_paths: BasePaths = None) -> dict:
    """The spec §3 object, cut where the tree first differs from base (any path: what resume
    needs), plus cut_cause, cut_ambiguous, changed_paths (at most MAX_CHANGED_PATHS, the full count
    in changed_paths_total) and pre_cut_turns_missing_usage. Paths go through redact.public_path:
    readable only if they existed at base (``base_paths``), else a path digest."""
    status, n, turn, changed = _first_change(turns, base, source_only=False)
    if status != "CUT":
        return _no_cut(CUT_FIELDS, status)
    assert turn is not None and n is not None
    cause, ambiguous = _cut_cause(turn, changed)
    target = turn.tool_args.get("path") if cause in EDIT_ACTIONS else (changed or [None])[0]
    reads, tokens, wall, missing = _pre(turns[:n], turn)
    return {
        "cut_occurred": True,
        "cut_turn_index": turn.index,
        "target_file": public_path(target, base_paths) if isinstance(target, str) else None,
        "is_test_file": is_test_path(target) if isinstance(target, str) else None,
        "pre_cut_read_actions": reads,
        "pre_cut_tokens_accum": tokens,
        "pre_cut_wall_seconds": wall,
        "cut_cause": cause,
        "cut_ambiguous": ambiguous,
        "changed_paths": public_paths(changed, base_paths),
        "changed_paths_total": len(changed),
        "pre_cut_turns_missing_usage": missing,
        "cut_status": "CUT",
    }


def first_source_edit(turns: list[TurnLog], base: TreeState, base_paths: BasePaths = None) -> dict:
    """The spec's original cut: the first turn whose observed changed paths include a source
    file (is_source_path, and not git-ignored unless an ignore file: _artifacts). It equals
    first_edit_cut unless a test, doc or run_tests artifact changed the tree first; resume always
    uses first_edit_cut."""
    status, n, turn, changed = _first_change(turns, base, source_only=True)
    if status != "CUT":
        return _no_cut(SOURCE_FIELDS, status)
    assert turn is not None and n is not None
    cause, _ = _cut_cause(turn, changed)
    path = str(turn.tool_args["path"]) if cause in EDIT_ACTIONS else changed[0]  # in changed
    reads, tokens, wall, missing = _pre(turns[:n], turn)
    return dict(
        zip(
            SOURCE_FIELDS,
            (True, turn.index, public_path(path, base_paths), cause, reads, tokens, wall, missing)
            + ("CUT",),
            strict=True,
        )
    )
