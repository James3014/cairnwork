"""The one path validator for ``allowed_files``: the Issue intake and ``check_task`` (submit)
both call ``path_refusal``, and ``check_candidate_commit`` re-runs ``is_protected`` on every
changed path, so no entry point can disagree about which paths a task may touch.

The list starts from the Nexus one (CI, governance, dependencies, task cards, test
configuration, conftest). Cairnwork's own gate files are added so a task cannot rewrite the
rules that judge it when Cairnwork works on its own repository: the size and Pyright gates,
the security policy, the lint/type configuration, and every package that decides, verifies,
notarizes or sandboxes (``src/cairnwork*``). The rules apply to any target repository: a
client repo that happens to hold such a path is refused too (fail closed, one rule set).
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import PurePosixPath
from typing import Any

SAFE_PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")
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
    # Cairnwork's own gate files (#77)
    "scripts/check_size.py",
    "scripts/pyright_gate.py",
    "SECURITY.md",
    "ruff.toml",
    "pyrightconfig.json",
    "src/cairnwork",
    "src/cairnwork_protocol",
    "src/cairnwork_sandbox",
    "src/cairnwork_verify",
)
PROTECTED_NAMES = (
    "conftest.py",
    "sitecustomize.py",
    "governance_baseline_manifest*.json",
    "taskfile*",
    "makefile",
    ".pre-commit-config*",
)


def path_refusal(path: Any) -> tuple[str, str] | None:
    """``(reason code, detail)`` when ``path`` may not be an allowed file, else None."""
    if not isinstance(path, str):
        return "TASK_ALLOWED_FILES_INVALID", repr(path)
    if is_protected(path):  # first, so ".github/x" is "protected" rather than "dotted"
        return "TASK_ALLOWED_FILES_PROTECTED", path
    if not SAFE_PATH.fullmatch(path) or PurePosixPath(path).as_posix() != path:
        return "TASK_ALLOWED_FILES_INVALID", repr(path)
    if any(part.startswith(".") for part in PurePosixPath(path).parts):  # a/.env, a/../b
        return "TASK_ALLOWED_FILES_INVALID", path
    return None


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
