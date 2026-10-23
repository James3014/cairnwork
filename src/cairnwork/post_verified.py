"""An opt-in hook run once the Issue runner has opened the PR of a Core-verified task.

The state ``config.json`` key ``post_verified_hook`` names an executable (generic: Core
never imports what it is). It gets the task, attempt and candidate as arguments and only
the few environment variables below, the GitHub token among them (never as an argument).
Its outcome is recorded in ``post-verified-hook.jsonl`` and alerted on stderr, and is never
a verdict: the labels, comment and PR are already final when it runs. Unset = nothing runs.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LOG_NAME = "post-verified-hook.jsonl"
TAIL_CHARS = 2000
TOKEN_VAR = "CAIRNWORK_GITHUB_TOKEN"
_PASSED = ("PATH", "HOME", "LANG", "LC_ALL", TOKEN_VAR)


def _env(gh: str) -> dict[str, str]:
    """Only what the hook needs; git authenticates a push as the runner's own push does."""
    env = {k: os.environ[k] for k in _PASSED if k in os.environ}
    env.update(
        GIT_TERMINAL_PROMPT="0",
        GIT_CONFIG_COUNT="2",
        GIT_CONFIG_KEY_0="credential.helper",
        GIT_CONFIG_VALUE_0="",
        GIT_CONFIG_KEY_1="credential.helper",
        GIT_CONFIG_VALUE_1=f"!{shlex.quote(gh)} auth git-credential",
    )
    return env


def _tail(text: str | bytes | None, token: str | None) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    text = text or ""
    if token:
        text = text.replace(token, "[redacted]")  # before cutting, so no half token is left
    return text[-TAIL_CHARS:]


def run_post_verified_hook(
    state: Path,
    config: dict[str, Any],
    *,
    task_id: str,
    attempt_id: str,
    candidate: str,
    clone: Path,
    repo_slug: str,
    gh: str,
) -> dict[str, Any] | None:
    """Run the configured hook; None when there is none. Never raises on the hook's account."""
    hook = config["post_verified_hook"]
    if hook is None:
        return None
    argv = [hook, "--state", str(state), "--task-id", task_id, "--attempt-id", attempt_id,
            "--candidate", candidate, "--repo", str(clone), "--repo-slug", repo_slug]  # fmt: skip
    token = os.environ.get(TOKEN_VAR) or None
    returncode: int | None = None
    error: str | None = None
    out = err = ""
    try:
        done = subprocess.run(
            argv, cwd="/", stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=config["post_verified_hook_seconds"], env=_env(gh), check=False,
        )  # fmt: skip
        returncode, out, err = done.returncode, done.stdout, done.stderr
    except subprocess.TimeoutExpired as exc:
        error, out, err = "TIMEOUT", exc.stdout, exc.stderr  # type: ignore[assignment]
    except (OSError, subprocess.SubprocessError) as exc:
        error = _tail(f"{type(exc).__name__}: {exc}", token)
    record = {
        "time": datetime.now(UTC).isoformat(),
        "task_id": task_id,
        "attempt_id": attempt_id,
        "candidate": candidate,
        "returncode": returncode,
        "error": error,
        "stdout_tail": _tail(out, token),
        "stderr_tail": _tail(err, token),
    }
    _append(state / LOG_NAME, record)
    ok = returncode == 0 and error is None
    reason = None if ok else (error or f"exit {returncode}")
    if reason:
        print(f"ALERT post_verified_hook task {task_id} attempt {attempt_id}: {reason}",
              file=sys.stderr)  # fmt: skip
    return {"returncode": returncode, "ok": ok, "reason": reason}


def _append(path: Path, record: dict[str, Any]) -> None:
    line = json.dumps(record, sort_keys=True) + "\n"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)
    except OSError as exc:
        print(f"ALERT post_verified_hook: cannot record to {path.name}: {type(exc).__name__}",
              file=sys.stderr)  # fmt: skip
