"""Run git with a fixed argv, no shell, and no inherited GIT_* environment.

Stripping GIT_* keeps a hook environment (GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE)
from pointing a command at another repository or index. Hooks, the fsmonitor
command and the untracked cache are disabled and replace refs are ignored.
These options are a second line of defence: workers never get a checkout that
shares Cairnwork's repository (see workspace.py), and Cairnwork never runs git
inside a worker's repository except to fetch its HEAD.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

GIT_TIMEOUT_SECONDS = 60
OID = re.compile(r"^[0-9a-f]{40}$")


# Options that config files cannot override: no hooks, no fsmonitor command, no
# untracked cache, and replace refs ignored so a commit is always what it is.
_SAFE_OPTIONS = (
    "--no-replace-objects",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.untrackedCache=false",
)


class GitCommandError(Exception):
    code = "GIT_COMMAND_FAILED"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


def run_git(repo: Path, *args: str, index_file: Path | None = None) -> str:
    """Run git in repo; index_file points git at a private index (GIT_INDEX_FILE)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    if index_file is not None:
        env["GIT_INDEX_FILE"] = str(index_file)
    try:
        done = subprocess.run(  # noqa: S603 - fixed git argv, no shell
            ["git", *_SAFE_OPTIONS, "-C", str(repo), *args],  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=GIT_TIMEOUT_SECONDS,
            env=env,
        )
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        raise GitCommandError(f"git {args[0]} failed") from exc
    return done.stdout


def hash_blob(repo: Path, data: bytes) -> str:
    """Store data as a blob in repo with no filters, from bytes the caller already read."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed git argv, no shell
            [
                "git",
                *_SAFE_OPTIONS,
                "-C",
                str(repo),
                "hash-object",
                "-w",
                "--no-filters",
                "--stdin",
            ],  # noqa: S607
            input=data,
            capture_output=True,
            check=True,
            timeout=GIT_TIMEOUT_SECONDS,
            env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitCommandError("git hash-object failed") from exc
    oid = done.stdout.decode("ascii", "replace").strip()
    if not OID.fullmatch(oid):
        raise GitCommandError(f"git hash-object returned {oid!r}")
    return oid
