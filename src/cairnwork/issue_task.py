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
from pathlib import PurePosixPath
from typing import Any

TASK_FENCE = re.compile(r"```eia-task[ \t]*\n(.*?)\n```", re.DOTALL)
ANY_FENCE = re.compile(r"```[A-Za-z0-9_-]*-contract\b")
SAFE_PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")
TEST_TARGET = re.compile(
    r"tests/(?:[A-Za-z0-9_\-]+/)*[A-Za-z0-9_\-]+\.py(?:::[A-Za-z0-9_\[\]\-]+)*"
)
MAX_ALLOWED_FILES = 10
KEYS = frozenset(
    {
        "difficulty",
        "allowed_files",
        "verification",
        "capability_ids",
        "capability_disposition",
        "summary",
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


def parse_task_block(body: str) -> IssueTask:
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
    summary = raw.get("summary", "")
    if not isinstance(summary, str):
        raise IssueRefused("TASK_SUMMARY_INVALID")
    return IssueTask(
        difficulty=difficulty,
        allowed_files=tuple(files),
        verifiers=tuple(pytest_argv(c) for c in commands),
        summary=" ".join(summary.split())[:400],
        block_sha256=hashlib.sha256(blocks[0].encode()).hexdigest(),
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


def pytest_argv(command: Any) -> tuple[str, ...]:
    """``python3 -m pytest [-q] [-p no:cacheprovider] tests/...``; anything else is refused."""
    try:
        argv = shlex.split(command) if isinstance(command, str) else []
    except ValueError as exc:
        raise IssueRefused("TASK_VERIFICATION_INVALID") from exc
    if argv[:1] == ["python"]:
        argv = ["python3", *argv[1:]]
    if argv[:3] != ["python3", "-m", "pytest"]:
        raise IssueRefused("TASK_VERIFICATION_UNSUPPORTED", command)
    rest, targets = argv[3:], []
    while rest:
        if rest[0] == "-q":
            rest = rest[1:]
        elif rest[:2] == ["-p", "no:cacheprovider"]:
            rest = rest[2:]
        elif TEST_TARGET.fullmatch(rest[0]):
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
    last = max(labeled, key=lambda e: str(e.get("at") or ""))
    if last.get("actor") not in trusted:
        return "QUEUE_LABELED_BY_UNTRUSTED"
    since = str(last.get("at") or "")
    for e in rows:
        if e.get("event") == "renamed" and str(e.get("at") or "") > since:
            if e.get("actor") not in trusted:
                return "TITLE_EDITED_BY_UNTRUSTED"
    if _gql_login(author) not in trusted:
        return "AUTHOR_UNTRUSTED"
    nodes = (edits or {}).get("nodes")
    total = (edits or {}).get("totalCount")
    if not isinstance(nodes, list) or type(total) is not int or total > len(nodes):
        return "EDIT_HISTORY_INCOMPLETE"
    for node in nodes:
        if (
            str(node.get("editedAt") or "") > since
            and _gql_login(node.get("editor")) not in trusted
        ):
            return "BODY_EDITED_BY_UNTRUSTED"
    return ""


def _gql_login(actor: Any) -> str | None:
    if not isinstance(actor, Mapping) or not isinstance(actor.get("login"), str):
        return None
    kind = actor.get("__typename")
    if kind == "Bot":
        return f"{actor['login']}[bot]"
    return actor["login"] if kind == "User" else None
