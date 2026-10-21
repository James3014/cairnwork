"""A queued GitHub Issue as a Cairnwork task: the ``eia-task`` block and who may queue it.

Pure functions, no network (issue_runner.py does the GitHub I/O). The rules follow the
Nexus-new EIA scheduler (docs/ops/eia_nightly.md, scripts/ops/eia_nightly.py at
Nexus-new 05c6a8c) so the same Issue means the same task in both systems:

* exactly one fenced ```eia-task JSON block; only the known keys;
* ``allowed_files``: 1 to 10 literal repo-relative paths, no ``.`` components, none
  protected (CI, governance, dependencies, task cards, test configuration, conftest);
* ``verification``: ``python3 -m pytest`` forms only (Cairnwork's verifier refuses the
  ``git diff --check`` shell form; such an Issue is refused here, not half-run);
* ``hard`` Issues are not taken.

The repository is public, so its text is untrusted unless ``queue_trusted`` holds:
the Issue author, the actor of the latest queue label event, and every title or body
edit after that event are trusted actors, and the edit history is complete.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import shlex
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

TASK_FENCE = re.compile(r"```eia-task[ \t]*\n(.*?)\n```", re.DOTALL)
ANY_FENCE = re.compile(r"```[A-Za-z0-9_-]*-contract\b")
SAFE_PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")
DEFAULT_TEST_ROOTS = ("tests",)
_ROOT = re.compile(r"[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-]+)*")
COMMIT = re.compile(r"[0-9a-f]{40}")
MAX_ALLOWED_FILES = 10
KEYS = frozenset(
    {
        "difficulty",
        "allowed_files",
        "verification",
        "capability_ids",
        "capability_disposition",
        "summary",
        "test_commit",
    }
)
PROTECTED_PATHS = (
    ".github",
    ".nexus-core",
    ".gitmodules",
    "docs/governance",
    "docs/agents",
    "docs/testing/test_impact_map.md",
    "tasks",
    "pyproject.toml",
    "uv.lock",
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    "MUSE_PROTO.md",
    "nexus/services/external_intelligence*",
    "scripts/ops/trusted_*",
    "scripts/ops/external_intelligence*",
    "scripts/ops/eia_nightly*",
    "scripts/ops/com.nexus.eia-nightly*",
    "pytest.ini",
    "setup.cfg",
    "tox.ini",
    "policy",
    "Makefile",
)
PROTECTED_NAMES = (
    "conftest.py",
    "sitecustomize.py",
    "governance_baseline_manifest*.json",
    "taskfile*",
    "makefile",
    ".pre-commit-config*",
)
_LATEST = datetime.max.replace(tzinfo=UTC)
TRUSTED_ACTORS = frozenset({"James3014", "james3014-nexus-agent[bot]"})


class IssueRefused(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True)
class IssueTask:
    difficulty: str
    allowed_files: tuple[str, ...]
    verifiers: tuple[tuple[str, ...], ...]
    summary: str
    block_sha256: str
    test_commit: str = ""  # deferred tests (cairnwork.deferred_tests)


def parse_task_block(body: str, roots: tuple[str, ...] = DEFAULT_TEST_ROOTS) -> IssueTask:
    blocks = TASK_FENCE.findall(body or "")
    if len(blocks) != 1:
        raise IssueRefused("TASK_BLOCK_REQUIRED", "exactly one ```eia-task block")
    if ANY_FENCE.search(body or ""):
        raise IssueRefused("TASK_BODY_HAS_FOREIGN_CONTRACT")
    try:
        raw = json.loads(blocks[0])
    except ValueError as exc:
        raise IssueRefused("TASK_BLOCK_JSON_INVALID") from exc
    if not isinstance(raw, dict) or set(raw) - KEYS:
        raise IssueRefused("TASK_BLOCK_KEYS_INVALID")
    difficulty = raw.get("difficulty")
    if difficulty not in ("easy", "medium"):
        raise IssueRefused("TASK_DIFFICULTY_NOT_TAKEN", str(difficulty))
    files = raw.get("allowed_files")
    if (
        not isinstance(files, list)
        or not 0 < len(files) <= MAX_ALLOWED_FILES
        or len(set(files)) != len(files)
    ):
        raise IssueRefused("TASK_ALLOWED_FILES_INVALID")
    for path in files:
        _check_path(path)
    commands = raw.get("verification")
    if not isinstance(commands, list) or not 0 < len(commands) <= 8:
        raise IssueRefused("TASK_VERIFICATION_INVALID")
    ids = raw.get("capability_ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i for i in ids):
        raise IssueRefused("TASK_CAPABILITY_IDS_INVALID")
    summary = raw.get("summary", "")
    if not isinstance(summary, str):
        raise IssueRefused("TASK_SUMMARY_INVALID")
    test_commit = raw.get("test_commit", "")
    if test_commit and not (isinstance(test_commit, str) and COMMIT.fullmatch(test_commit)):
        raise IssueRefused("TASK_TEST_COMMIT_INVALID")
    return IssueTask(
        difficulty=difficulty,
        allowed_files=tuple(files),
        verifiers=tuple(pytest_argv(c, roots) for c in commands),
        summary=" ".join(summary.split())[:400],
        block_sha256=hashlib.sha256(blocks[0].encode()).hexdigest(),
        test_commit=test_commit,
    )


def _check_path(path: Any) -> None:
    if not isinstance(path, str) or not SAFE_PATH.fullmatch(path):
        raise IssueRefused("TASK_ALLOWED_FILES_INVALID", repr(path))
    parts = PurePosixPath(path).parts
    if PurePosixPath(path).as_posix() != path or any(p.startswith(".") for p in parts):
        raise IssueRefused("TASK_ALLOWED_FILES_INVALID", path)
    if is_protected(path):
        raise IssueRefused("TASK_ALLOWED_FILES_PROTECTED", path)


def is_protected(path: str) -> bool:
    low = path.casefold()
    name = PurePosixPath(low).name
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in PROTECTED_NAMES):
        return True
    for rule in (r.casefold() for r in PROTECTED_PATHS):
        if rule.endswith("*"):
            if low.startswith(rule[:-1]):
                return True
        elif low == rule or low.startswith(rule + "/"):
            return True
    return False


def test_target(roots: tuple[str, ...]) -> re.Pattern[str]:
    """A verifier target: a .py file under one of the test roots, with optional ::node ids."""
    top = "|".join(map(re.escape, roots))
    name = r"[A-Za-z0-9_\-]+"
    return re.compile(rf"(?:{top})/(?:{name}/)*{name}\.py(?:::[A-Za-z0-9_\[\]\-]+)*")


def check_test_roots(roots: Any) -> tuple[str, ...]:
    """The state config's ``test_roots``: 1 to 8 distinct directories, written as clean
    relative paths of plain names (no glob, dot or empty part), outside protected paths."""
    if (
        not isinstance(roots, list)
        or not 0 < len(roots) <= 8
        or len(set(map(str, roots))) < len(roots)
    ):
        raise ValueError(f"test_roots must be 1-8 distinct directory paths, got {roots!r}")
    for root in roots:
        if not (isinstance(root, str) and _ROOT.fullmatch(root)) or is_protected(root):
            raise ValueError(f"test_roots entry {root!r} is not a clean unprotected directory")
    return tuple(roots)


def pytest_argv(command: Any, roots: tuple[str, ...] = DEFAULT_TEST_ROOTS) -> tuple[str, ...]:
    """``python3 -m pytest [-q] [-p no:cacheprovider] <root>/...``; anything else is refused."""
    try:
        argv = shlex.split(command) if isinstance(command, str) else []
    except ValueError as exc:
        raise IssueRefused("TASK_VERIFICATION_INVALID") from exc
    if argv[:1] == ["python"]:
        argv = ["python3", *argv[1:]]
    if argv[:3] != ["python3", "-m", "pytest"]:
        raise IssueRefused("TASK_VERIFICATION_UNSUPPORTED", command)
    rest, targets, target = argv[3:], [], test_target(roots)
    while rest:
        if rest[0] == "-q":
            rest = rest[1:]
        elif rest[:2] == ["-p", "no:cacheprovider"]:
            rest = rest[2:]
        elif target.fullmatch(rest[0]):
            targets.append(rest.pop(0))
        else:
            raise IssueRefused("TASK_VERIFICATION_UNSUPPORTED", command)
    if not targets:
        raise IssueRefused("TASK_VERIFICATION_UNSUPPORTED", command)
    return ("python3", "-m", "pytest", "-q", *targets)


def queue_trusted(
    *,
    label: str,
    events: Iterable[Mapping[str, Any]],
    author: Mapping[str, Any] | None,
    edits: Mapping[str, Any] | None,
    trusted: frozenset[str] = TRUSTED_ACTORS,
) -> str:
    """Return "" when trusted, else the reason. events: REST timeline rows with
    event/label/actor/at; author and edits: the GraphQL issue fields."""
    rows = list(events)
    labeled = [e for e in rows if e.get("event") == "labeled" and e.get("label") == label]
    if not labeled:
        return "QUEUE_LABEL_EVENT_MISSING"
    last = max(labeled, key=lambda e: _when(e.get("at")))
    if last.get("actor") not in trusted:
        return "QUEUE_LABELED_BY_UNTRUSTED"
    since = _when(last.get("at"))
    if since == _LATEST:
        return "QUEUE_LABEL_TIME_UNREADABLE"
    for e in rows:  # same-second changes count as after the label (conservative)
        if e.get("event") == "renamed" and _when(e.get("at")) >= since:
            if e.get("actor") not in trusted:
                return "TITLE_EDITED_BY_UNTRUSTED"
    if _gql_login(author) not in trusted:
        return "AUTHOR_UNTRUSTED"
    nodes = (edits or {}).get("nodes")
    total = (edits or {}).get("totalCount")
    if not isinstance(nodes, list) or type(total) is not int or total > len(nodes):
        return "EDIT_HISTORY_INCOMPLETE"
    for node in nodes:
        if _when(node.get("editedAt")) >= since and _gql_login(node.get("editor")) not in trusted:
            return "BODY_EDITED_BY_UNTRUSTED"
    return ""


def _when(value: Any) -> datetime:
    """A GitHub timestamp; anything unreadable sorts as 'latest' (fails closed)."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return _LATEST
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _gql_login(actor: Any) -> str | None:
    if not isinstance(actor, Mapping) or not isinstance(actor.get("login"), str):
        return None
    kind = actor.get("__typename")
    if kind == "Bot":
        return f"{actor['login']}[bot]"
    return actor["login"] if kind == "User" else None
