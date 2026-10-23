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

To keep honest code out of the net (#221): pure comment lines are skipped; the rules that only
matter where pytest runs the candidate's test code (``sys.modules`` writes, ``_pytest`` internals,
pytest hook implementations) apply only to test code (``conftest.py``, ``test_*.py``,
``*_test.py`` and files under a ``tests``/``test``/``testing`` directory), so a library's own
compatibility shims and pytest plugins pass; there, a literal stub of an unrelated module
(``sys.modules["dep"] = MagicMock()``), ``sys.modules[__name__] = ...`` and a type-hint import of
a public fixture class from ``_pytest`` are allowed. A library file that a conftest loads as a
plugin is therefore not checked for those three rules: a known gap of this raised bar.
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
    allow: re.Pattern[str] | None = None  # a whole line of this shape is honest usage
    tests_only: bool = False  # ordinary in library code; only checked in test code (_is_test_path)


def _rule(
    name: str, regex: str, *, skip_in_conftest: bool = False, allow: str = "", tests_only=False
) -> Rule:
    compiled = re.compile(allow) if allow else None
    return Rule(name, re.compile(regex), skip_in_conftest, compiled, tests_only)


# Public pytest fixture types that honest code imports from ``_pytest`` for type hints only.
# Classes whose methods a forgery would patch (Item, Config, reports, runner) are not here.
_HINT = (
    r"(?:CaptureFixture|FixtureRequest|LogCaptureFixture|MonkeyPatch|Pytester|TempPathFactory"
    r"|TempdirFactory|Testdir)(?:\s+as\s+\w+)?"
)
_HINTS = rf"{_HINT}(?:\s*,\s*{_HINT})*"
_PYTEST_HINT_IMPORT = (  # one complete line; an open "(" continuing on the next line is not allowed
    rf"^\s*from\s+_pytest(?:\.\w+)+\s+import\s+(?:{_HINTS}|\(\s*{_HINTS}\s*,?\s*\))\s*(?:#.*)?$"
)
_COMMENT_ONLY = re.compile(r"^\s*#")
# sys.modules entries a forgery would swap; a literal key naming any other module is a test stub.
_SENSITIVE_MODULE = (
    r"(?:_?pytest\w*|pluggy|py|_?cairnwork\w*|sys|os|builtins|importlib|gc|conftest|__main__)"
)
_STUB_NAME = (
    rf"(?:'(?!{_SENSITIVE_MODULE}(?:\.|'))[\w.]+'|\"(?!{_SENSITIVE_MODULE}(?:\.|\"))[\w.]+\")"
)
_STUB_KEY = rf"\[\s*{_STUB_NAME}\s*\]"
# One statement on the line: assign, delete, pop or setdefault one literal stub key.
_SYS_MODULES_STUB = (
    rf"^\s*(?:sys\s*\.\s*modules\s*{_STUB_KEY}\s*=(?!=)[^;]*"
    rf"|del\s+sys\s*\.\s*modules\s*{_STUB_KEY}"
    rf"|sys\s*\.\s*modules\s*\.\s*pop\s*\(\s*{_STUB_NAME}\s*(?:,\s*None\s*)?\)"
    rf"|sys\s*\.\s*modules\s*\.\s*setdefault\s*\(\s*{_STUB_NAME}\s*,[^;]*)\s*(?:#.*)?$"
)
_TEST_DIRS = frozenset({"tests", "test", "testing"})


def _is_test_path(path: str) -> bool:
    """conftest.py, test_*.py / *_test.py, or any file under a tests/ test/ testing/ directory."""
    parts = path.split("/")
    name = parts[-1]
    if name == "conftest.py" or name.startswith("test_") or name.endswith("_test.py"):
        return True
    return any(part in _TEST_DIRS for part in parts[:-1])


RULES: tuple[Rule, ...] = (
    _rule("os_exit", r"\bos\s*\.\s*_exit\b|\bfrom\s+os\s+import\b[^#\n]*\b_exit\b"),
    _rule(
        "cairnwork_env",
        r"(?=.*\bCAIRNWORK_)(?=.*(?:\benviron\b|\bgetenv\b|\benvb\b))",
    ),
    _rule("gc_walk", r"\bgc\s*\.\s*(?:get_objects|get_referrers|get_referents)\b"),
    _rule(
        "sys_modules_write",
        r"\bsys\s*\.\s*modules\s*\[(?!\s*__name__\s*\])[^\]]*\]\s*=(?!=)"
        r"|\bdel\s+sys\s*\.\s*modules\b"
        r"|\bsys\s*\.\s*modules\s*\.\s*(?:pop|popitem|update|setdefault|clear)\s*\(",
        allow=_SYS_MODULES_STUB,
        tests_only=True,
    ),
    _rule(
        "pytest_internals",
        r"\b_pytest\b|\bpluginmanager\b",
        allow=_PYTEST_HINT_IMPORT,
        tests_only=True,
    ),
    _rule(
        "pytest_hook_impl",
        r"^\s*(?:async\s+)?def\s+pytest_(?:runtest|sessionfinish|sessionstart|collection|configure"
        r"|unconfigure|report|terminal_summary|exception_interact)\w*"
        r"|\bpytest\s*\.\s*hookimpl\b",
        skip_in_conftest=True,
        tests_only=True,
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
    in_tests = _is_test_path(path)
    found = []
    for number, text in lines:
        if _COMMENT_ONLY.match(text):
            continue
        for rule in RULES:
            if (rule.skip_in_conftest and in_conftest) or (rule.tests_only and not in_tests):
                continue
            if rule.allow is not None and rule.allow.match(text):
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
            target = _unquote(raw[4:].rstrip("\t"))  # git appends a tab to a path with a space
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
