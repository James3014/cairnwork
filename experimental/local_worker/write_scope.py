"""Optional write scope of the local worker arm (#88 follow-up), off unless set.

The K arm's dispatcher takes one ``--write-path`` per allowed path and refuses writes outside them
as they happen; without a like limit, scope violations would count against the local arm alone. A
scope is a list of repo-relative entries, each a file or a directory, with K's match (Nexus
``direct_operation_journal._is_path_in_scope``): a path is in scope when it equals an entry or
lies under one (entry + "/" prefix), compared case-sensitively.

The wrapper enforces it (local-worker-tool ``write --scope``), not this module: only the worker may
look at the checkout, and only the wrapper sees the physical path a write lands on (symlinks and
case variants resolved), with Seatbelt allowing writes only under the entries. Here the entries are
only checked to be canonical, and turned into the wrapper's arguments. Unset (None) changes
nothing: no argument, no manifest key, no line field.
"""

from __future__ import annotations

from collections.abc import Iterable

WRITE_SCOPE_RULE = (  # recorded in manifest.json when a corpus runs with a write scope
    "write_scope: write_file and edit_file are refused (exit 64, error_category REFUSED, nothing"
    " written; the episode goes on) unless the physical repo-relative path the write lands on"
    " (symlinks and case resolved by the worker) equals an entry of the episode's write_scope or"
    " lies under one (entry + '/' prefix, case-sensitive): the K arm's --write-path match. Entries"
    " are canonical relpaths taken literally. run_tests side effects are not limited by it."
)
_BAD_CHARS = ('"', "\\", "\n", "\r", "\0")


def canonical_entry(raw: str) -> str:
    """``raw`` without trailing slashes, if it is a canonical relative path; else ValueError."""
    entry = raw.rstrip("/") if isinstance(raw, str) else ""
    parts = entry.split("/")
    bad = not entry or entry.startswith(("/", "-")) or any(c in entry for c in _BAD_CHARS)
    bad = bad or any(p in ("", ".", "..") or p.lower() == ".git" for p in parts)
    if bad:
        raise ValueError(f"write scope entry {raw!r} is not a canonical repo-relative path")
    return entry


def normalize(paths: Iterable[str] | None) -> tuple[str, ...] | None:
    """The scope as sorted unique canonical entries; None stays None (no scope). An empty list
    is refused: K reads it as no limit, which a caller asking for a scope did not mean."""
    if paths is None:
        return None
    scope = tuple(sorted({canonical_entry(p) for p in paths}))
    if not scope:
        raise ValueError("an empty write scope: leave it unset for no limit")
    return scope


def wrapper_args(scope: tuple[str, ...] | None) -> list[str]:
    """The arguments after a write's relpath: none without a scope."""
    return [] if scope is None else ["--scope", *scope]
