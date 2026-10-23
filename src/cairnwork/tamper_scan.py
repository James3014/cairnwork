"""Static scan of a candidate patch for the APIs a verdict forgery needs (``VERIFIER_TAMPER_API``).

A candidate's code runs inside the pytest process that produces the trusted verdict (see
report_guard.py), so it can in principle exit early, read the guard's hand-over, walk the
interpreter's objects or reach into pytest. This module refuses a patch whose added ``.py``
lines mention those APIs, before any candidate code runs.

This raises the bar; it is not a boundary. It reads single added lines with regular
expressions, so a name built from strings (``getattr(os, "_" + "exit")``), a split line, an alias
(``import os as o`` then ``o._exit``) or a non-``.py`` file that is imported later all pass. It
catches the straightforward forms and the PoCs seen so far, and it fails closed with a
dedicated reason so an operator can see why. The rule list is the one place to change.
"""

from __future__ import annotations

import codecs
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path

from cairnwork.gitcmd import run_git

CODE = "VERIFIER_TAMPER_API"


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    skip_in_conftest: bool = False  # hook implementations are normal in a conftest.py


def _rule(name: str, regex: str, *, skip_in_conftest: bool = False) -> Rule:
    return Rule(name, re.compile(regex), skip_in_conftest)


RULES: tuple[Rule, ...] = (
    _rule("os_exit", r"\bos\s*\.\s*_exit\b|\bfrom\s+os\s+import\b[^#\n]*\b_exit\b"),
    _rule(
        "cairnwork_env",
        r"(?=.*\bCAIRNWORK_)(?=.*(?:\benviron\b|\bgetenv\b|\benvb\b))",
    ),
    _rule("gc_walk", r"\bgc\s*\.\s*(?:get_objects|get_referrers|get_referents)\b"),
    _rule(
        "sys_modules_write",
        r"\bsys\s*\.\s*modules\s*\[[^\]]*\]\s*=(?!=)"
        r"|\bdel\s+sys\s*\.\s*modules\b"
        r"|\bsys\s*\.\s*modules\s*\.\s*(?:pop|popitem|update|setdefault|clear)\s*\(",
    ),
    _rule("pytest_internals", r"\b_pytest\b|\bpluginmanager\b"),
    _rule(
        "pytest_hook_impl",
        r"^\s*(?:async\s+)?def\s+pytest_(?:runtest|sessionfinish|sessionstart|collection|configure"
        r"|unconfigure|report|terminal_summary|exception_interact)\w*"
        r"|\bpytest\s*\.\s*hookimpl\b",
        skip_in_conftest=True,
    ),
    _rule("guard_module", r"_cairnwork_report_guard"),
    _rule("fd_listing", r"/proc/(?:self|thread-self|\d+)/fd\b|/dev/fd\b"),
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line} [{self.rule}]"


def scan_added_lines(path: str, lines: list[tuple[int, str]]) -> list[Finding]:
    """Findings for the added ``(line number, text)`` pairs of one file (``.py`` files only)."""
    if not path.endswith(".py"):
        return []
    in_conftest = posixpath.basename(path) == "conftest.py"
    found = []
    for number, text in lines:
        for rule in RULES:
            if rule.skip_in_conftest and in_conftest:
                continue
            if rule.pattern.search(text):
                found.append(Finding(path, number, rule.name))
    return found


def added_lines(diff: str) -> dict[str, list[tuple[int, str]]]:
    """The added lines of a ``git diff -U0`` text, by new path with their new line numbers."""
    files: dict[str, list[tuple[int, str]]] = {}
    current, number = None, 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            target = _unquote(raw[4:])
            current = target[2:] if target.startswith("b/") else None
            if current is not None:
                files.setdefault(current, [])
        elif raw.startswith("@@"):
            match = re.search(r"\+(\d+)", raw)
            number = int(match.group(1)) if match else 0
        elif current is not None and raw.startswith("+"):
            files[current].append((number, raw[1:]))
            number += 1
    return files


def _unquote(name: str) -> str:
    """git C-quotes a path with special characters (``"b/a\\tb.py"``, octal escapes for bytes)."""
    if len(name) < 2 or not (name.startswith('"') and name.endswith('"')):
        return name
    raw = name[1:-1].encode("utf-8", "surrogateescape")
    return codecs.escape_decode(raw)[0].decode("utf-8", "surrogateescape")


def scan_patch(repo: Path, base: str, head: str) -> list[Finding]:
    """Scan what ``head`` adds on top of ``base`` (both commits already in repo)."""
    diff = run_git(
        repo, "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames", "-U0",
        "--src-prefix=a/", "--dst-prefix=b/", base, head,
    )  # fmt: skip
    return [
        finding
        for path, lines in added_lines(diff).items()
        for finding in scan_added_lines(path, lines)
    ]
