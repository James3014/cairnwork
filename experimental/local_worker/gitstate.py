"""Parsing the worker's two fixed git queries for the local worker loop (#88, Plan v2 point 4).

``status`` is ``git status --porcelain=v2 --branch -z -uall --renames --ignored=matching`` (ignored
files are listed too, so a write to an ignored path still changes the observed tree); ``numstat``
is one line, ``PATCH <added> <removed> <binary_files_changed>``, counted from the ``--text -U0``
patch against HEAD with untracked and ignored files included. Output that does not parse is never
guessed into a tree or a count: it is reported as not observed.
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


_PATCH = re.compile(rb"PATCH (\d{1,18}) (\d{1,18}) (\d{1,18})\n")


def parse_numstat(raw: bytes) -> tuple[int, int, int] | None:
    """(lines added, lines removed, binary_files_changed) from the wrapper's numstat line.

    Claude review of #239, NB2: ``git diff --numstat`` shows a file it takes for binary (one NUL
    byte is enough) as ``-`` lines, and ``--text`` does not change that, so summing numstat let
    one NUL byte hide a whole file's patch. The wrapper therefore counts added and removed lines
    from the hunk headers of ``git diff --cached --text -U0`` (every file's lines, binary or not)
    and also reports how many files numstat would have shown as binary, so a reader can see
    which counts would have been hidden. Anything but exactly that one line is None."""
    m = _PATCH.fullmatch(raw)
    if m is None:
        return None
    added, removed, binary = (int(g) for g in m.groups())
    return added, removed, binary
