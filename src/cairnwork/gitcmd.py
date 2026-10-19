"""Run git with a fixed argv, no shell, and no inherited GIT_* environment.

Stripping GIT_* keeps a hook environment (GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE)
from pointing a command at another repository or index. Hooks are disabled
(core.hooksPath=/dev/null): attempt worktrees share the repository's hooks
directory, so a worker could otherwise plant a hook that Cairnwork would run.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

GIT_TIMEOUT_SECONDS = 60
OID = re.compile(r"^[0-9a-f]{40}$")


class GitCommandError(Exception):
    code = "GIT_COMMAND_FAILED"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


def run_git(repo: Path, *args: str) -> str:
    try:
        done = subprocess.run(  # noqa: S603 - fixed git argv, no shell
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=GIT_TIMEOUT_SECONDS,
            env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        )
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        raise GitCommandError(f"git {args[0]} failed") from exc
    return done.stdout
