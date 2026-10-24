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
from typing import Any

GIT_TIMEOUT_SECONDS = 60
NETWORK_TIMEOUT_SECONDS = 300  # fetch and push over a network; local commands keep the default
STDERR_TAIL_CHARS = 400
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


# Greedy to the last '@' in the run free of space and tab (newlines count as part of the run, so
# one planted in the userinfo cannot split it): a password may itself hold '@' or '/'.
# Fail-closed: an '@' later in a path also removes the text before it.
_URL_USERINFO = re.compile(r"(?<=://)[^ \t]*@")
_CONTROL = re.compile(r"[^\x20-\x7e\n\u00a0-\U0010ffff]|[\u2028\u2029\u202a-\u202e\u2066-\u2069]")


def stderr_tail(raw: str | bytes | None) -> str:
    """Last STDERR_TAIL_CHARS of git's stderr, one line per line, safe to put in an error.

    URL credentials (``https://user:token@host``) are removed and control characters
    (terminal escapes, CR overwrites) are dropped, so the text can go to a log or an Issue.
    Control characters go first, so none can split a credential before it is redacted. The
    redaction is greedy to the last '@' of a token, so a URL with '@' in its path loses the
    text before it: an accepted trade-off, redact rather than leak.
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    text = _URL_USERINFO.sub("", _CONTROL.sub("", raw or "")).strip()
    return text.replace("\n", " | ")[-STDERR_TAIL_CHARS:]


def _subcommand(args: tuple[str, ...]) -> str:
    """The git subcommand in args, skipping ``-c <name=value>``, ``-C <path>`` and other options."""
    it = iter(args)
    for arg in it:
        if arg in ("-c", "-C"):
            next(it, None)
        elif not arg.startswith("-"):
            return arg
    return args[0] if args else "command"


class GitCommandError(Exception):
    code = "GIT_COMMAND_FAILED"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


def run_git(
    repo: Path,
    *args: str,
    index_file: Path | None = None,
    isolate_config: bool = False,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> str:
    """Run git in repo; index_file points git at a private index (GIT_INDEX_FILE).

    isolate_config ignores the operator's global and system git config for this call
    (``GIT_CONFIG_GLOBAL``/``GIT_CONFIG_SYSTEM`` → /dev/null). Stripping GIT_* does not
    stop git reading ``~/.gitconfig``/``/etc/gitconfig``, where a setting such as
    ``gc.recentObjectsHook`` runs a command during a prune — which ``core.hooksPath``
    does not cover. Use it for maintenance commands (gc, prune) on a worker's clone.

    timeout is in seconds; pass NETWORK_TIMEOUT_SECONDS for a fetch or push over a network.
    A failure carries a bounded, sanitised tail of git's stderr (see stderr_tail).
    """
    return _exec(repo, args, index_file, isolate_config, timeout, text=True)


def run_git_bytes(repo: Path, *args: str, timeout: float = GIT_TIMEOUT_SECONDS) -> bytes:
    """Like run_git, but stdout is raw bytes: no decoding and no newline translation.

    A failure's ``CalledProcessError`` (``__cause__``) then carries bytes in ``stdout``.
    """
    return _exec(repo, args, None, False, timeout, text=False)


def _exec(
    repo: Path,
    args: tuple[str, ...],
    index_file: Path | None,
    isolate_config: bool,
    timeout: float,
    *,
    text: bool,
) -> Any:  # str when text, else bytes
    if not timeout or timeout <= 0:
        raise ValueError(f"timeout must be a positive number of seconds, got {timeout!r}")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    if index_file is not None:
        env["GIT_INDEX_FILE"] = str(index_file)
    if isolate_config:
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
    try:
        done = subprocess.run(  # noqa: S603 - fixed git argv, no shell
            ["git", *_SAFE_OPTIONS, "-C", str(repo), *args],  # noqa: S607
            check=True,
            capture_output=True,
            text=text,
            encoding="utf-8" if text else None,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        tail = stderr_tail(getattr(exc, "stderr", None))
        raise GitCommandError(
            f"git {_subcommand(args)} failed" + (f": {tail}" if tail else "")
        ) from exc
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
