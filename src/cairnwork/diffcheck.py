"""Name the ``git diff --check`` findings in a DIFF_CHECK_FAILED refusal (#227).

What can reach the message: only the text of a header line ``<path>:<line>: <kind>`` that git
printed, where ``path`` is exactly one of the change's own paths and ``kind`` is a known git
``--check`` message (or ", "-joined known stems ending in one "."). At most 5 entries, each
stripped of control, format and line-separator characters and cut at 120 characters.

What cannot: candidate text. git's output is read as raw bytes and split on b"\n" only, so a CR
embedded in a candidate line stays inside its ``+content`` line, which is never a header. A
forged header needs a newline, which a content line cannot hold. Output that is not strict UTF-8
or holds no valid entry gives the old text unchanged.
"""

from __future__ import annotations

import re
import subprocess
import unicodedata
from collections.abc import Collection
from typing import Any

RULE = "git-diff-check"
MAX_ENTRIES = 5
MAX_ENTRY_CHARS = 120
# Stems of git's ws_check_emit messages (whitespace.c). git joins several errors of one line
# as ``a, b.`` (one final period); the conflict marker is its own line without a period.
KINDS = frozenset(
    {
        "trailing whitespace",
        "space before tab in indent",
        "indent with spaces",
        "tab in indent",
        "new blank line at EOF",
    }
)
CONFLICT_MARKER = "leftover conflict marker"
_HEADER = re.compile(r"([^+\n].*):(\d+): ([^\n]+)")
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def _known_kind(kind: str) -> bool:
    if kind == CONFLICT_MARKER:
        return True
    stems = kind[:-1].split(", ")
    return kind.endswith(".") and all(stem in KINDS for stem in stems)


def _clean(text: str) -> str:
    return "".join(c for c in text if unicodedata.category(c) not in _UNSAFE_CATEGORIES)


def diff_check_detail(base: str, cause: BaseException | None, paths: Collection[str]) -> str:
    """``base`` plus ``; rule=git-diff-check; <path>:<line>: <kind>`` (at most 5), or ``base``."""
    raw: Any = cause.stdout if isinstance(cause, subprocess.CalledProcessError) else None
    try:
        out = raw.decode("utf-8") if isinstance(raw, bytes) else None
    except UnicodeDecodeError:
        out = None
    if out is None:
        return base
    known = frozenset(paths)
    found = [
        m.group(0)
        for line in out.split("\n")
        if (m := _HEADER.fullmatch(line)) and m.group(1) in known and _known_kind(m.group(3))
    ]
    if not found:
        return base
    entries = [_clean(entry)[:MAX_ENTRY_CHARS] for entry in found[:MAX_ENTRIES]]
    more = f"; +{len(found) - MAX_ENTRIES} more" if len(found) > MAX_ENTRIES else ""
    return f"{base}; rule={RULE}; " + "; ".join(entries) + more
