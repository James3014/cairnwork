"""Name the ``git diff --check`` findings in a DIFF_CHECK_FAILED refusal (#227).

Only git's ``path:line: kind`` header lines are read. The ``+content`` lines that follow them
are candidate text and never leave this module.
"""

from __future__ import annotations

import re
import subprocess

RULE = "git-diff-check"
MAX_ENTRIES = 5
MAX_ENTRY_CHARS = 120
_HEADER = re.compile(r"([^+\n].*?):(\d+): ([^\n]+)")
_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f؜‎‏  ‪-‮⁦-⁩]")


def diff_check_detail(base: str, cause: BaseException | None) -> str:
    """``base`` plus ``; rule=git-diff-check; <path>:<line>: <kind>`` (at most 5), or ``base``."""
    out = cause.stdout if isinstance(cause, subprocess.CalledProcessError) else None
    if not isinstance(out, str):  # undecodable output has no stdout: keep the old text
        return base
    found = [m.group(0) for line in out.split("\n") if (m := _HEADER.fullmatch(line))]
    if not found:
        return base
    entries = [_UNSAFE.sub("", entry)[:MAX_ENTRY_CHARS] for entry in found[:MAX_ENTRIES]]
    more = f"; +{len(found) - MAX_ENTRIES} more" if len(found) > MAX_ENTRIES else ""
    return f"{base}; rule={RULE}; " + "; ".join(entries) + more
