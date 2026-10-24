"""Name the ``git diff --check`` findings in a DIFF_CHECK_FAILED refusal (#227).

An entry is kept only when its path is one of the change's paths and its kind is one of git's
own ``--check`` messages, so no free candidate text (a ``+content`` line, even one that looks
like a header after a colour code or an embedded CR) can reach the message.
"""

from __future__ import annotations

import re
import subprocess
import unicodedata
from collections.abc import Collection

RULE = "git-diff-check"
MAX_ENTRIES = 5
MAX_ENTRY_CHARS = 120
# The messages of git's ws_check_emit (whitespace.c) as printed by ``git diff --check``.
KINDS = frozenset(
    {
        "trailing whitespace.",
        "space before tab in indent.",
        "indent with spaces.",
        "tab in indent.",
        "new blank line at EOF.",
        "leftover conflict marker",
    }
)
_HEADER = re.compile(r"([^+\n].*):(\d+): ([^\n]+)")
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def _clean(text: str) -> str:
    return "".join(c for c in text if unicodedata.category(c) not in _UNSAFE_CATEGORIES)


def diff_check_detail(base: str, cause: BaseException | None, paths: Collection[str]) -> str:
    """``base`` plus ``; rule=git-diff-check; <path>:<line>: <kind>`` (at most 5), or ``base``."""
    out = cause.stdout if isinstance(cause, subprocess.CalledProcessError) else None
    if not isinstance(out, str):  # undecodable output has no stdout: keep the old text
        return base
    known = frozenset(paths)
    found = [
        m.group(0)
        for line in out.split("\n")
        if (m := _HEADER.fullmatch(line)) and m.group(1) in known and m.group(3) in KINDS
    ]
    if not found:
        return base
    entries = [_clean(entry)[:MAX_ENTRY_CHARS] for entry in found[:MAX_ENTRIES]]
    more = f"; +{len(found) - MAX_ENTRIES} more" if len(found) > MAX_ENTRIES else ""
    return f"{base}; rule={RULE}; " + "; ".join(entries) + more
