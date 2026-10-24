"""Parsing the worker's two fixed git queries for the local worker loop (#88, Plan v2 point 4).

``status`` is ``git status --porcelain=v2 --branch -z -uall --renames --ignored=matching`` (ignored
files are listed too, so a write to an ignored path still changes the observed tree); ``numstat``
is ``git diff --numstat -z`` against HEAD with untracked files included. Output that does not parse
is never guessed into a tree: it is reported as not observed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class TreeState:
    """One observed ``git status``: HEAD and the porcelain v2 entries (sorted), or not observed."""

    observed: bool
    head: str | None = None
    entries: tuple[str, ...] = ()

    def paths(self) -> list[str]:
        return sorted({p for e in self.entries for p in _entry_paths(e)})


def _entry_paths(entry: str) -> list[str]:
    kind = entry[:1]
    if kind in ("?", "!"):
        return [entry[2:]]
    if kind == "1":
        return [entry.split(" ", 8)[-1]]
    if kind == "2":
        head, _, orig = entry.partition("\0")
        return [head.split(" ", 9)[-1], orig]
    if kind == "u":
        return [entry.split(" ", 10)[-1]]
    return []


def parse_status(raw: bytes) -> TreeState:
    """Parse ``git status --porcelain=v2 --branch -z`` output (rename entries carry two paths)."""
    fields = raw.decode("utf-8", errors="surrogateescape").split("\0")
    head: str | None = None
    entries: list[str] = []
    i = 0
    while i < len(fields):
        f = fields[i]
        if f.startswith("# branch.oid "):
            oid = f.removeprefix("# branch.oid ")
            head = oid if re.fullmatch(r"[0-9a-f]{40,64}", oid) else None
        elif f[:2] in ("1 ", "u ", "? ", "! "):
            entries.append(f)
        elif f.startswith("2 ") and i + 1 < len(fields):
            entries.append(f + "\0" + fields[i + 1])
            i += 1
        elif f and not f.startswith("# "):
            return TreeState(observed=False)  # not git porcelain: never guess a tree
        i += 1
    return TreeState(observed=True, head=head, entries=tuple(sorted(entries)))


def parse_numstat(raw: bytes) -> tuple[int, int] | None:
    """Sum ``git diff --numstat -z`` (binary files count 0); None if a record does not parse."""
    added = removed = 0
    for rec in raw.decode("utf-8", errors="surrogateescape").split("\0"):
        if not rec:
            continue
        m = re.match(r"(\d+|-)\t(\d+|-)\t", rec)
        if not m:
            return None
        added += int(m.group(1)) if m.group(1) != "-" else 0
        removed += int(m.group(2)) if m.group(2) != "-" else 0
    return added, removed
