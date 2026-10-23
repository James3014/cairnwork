"""An opt-in hook run once the Issue runner has opened the PR of a Core-verified task.

The state ``config.json`` key ``post_verified_hook`` names an executable (generic: Core
never imports what it is). It gets the task, attempt and candidate as arguments and only
the few environment variables below, the GitHub token among them (never as an argument).
Its outcome is recorded in ``post-verified-hook.jsonl`` and alerted on stderr, and is never
a verdict: the labels, comment, PR and the runner's result are already final when it runs.
Unset = nothing runs.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LOG_NAME = "post-verified-hook.jsonl"
TAIL_CHARS = 2000
CLEANUP_SECONDS = 5  # after a timeout kill, for pipes a detached descendant still holds
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
        returncode, out, err = _run(argv, config["post_verified_hook_seconds"], _env(gh))
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
        alert(f"task {task_id} attempt {attempt_id}: {reason}")
    return {"returncode": returncode, "ok": ok, "reason": reason}


def alert(message: str) -> None:
    """One stderr line; never raises, and a broken stderr cannot change the exit code."""
    try:
        print(f"ALERT post_verified_hook {message}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 - nowhere left to report to
        try:  # the unflushed line would fail again at exit (status 120): point fd 2 at devnull
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stderr.fileno())
            os.close(devnull)
        except Exception:  # noqa: BLE001, S110 - e.g. an in-memory stderr with no descriptor
            pass


def _run(argv: list[str], seconds: int, env: dict[str, str]) -> tuple[int, bytes, bytes]:
    """Its own session, so a timeout kills what the hook started; cleanup is bounded too.

    Output stays bytes: _tail decodes it leniently, so no output can lose the record.
    """
    with subprocess.Popen(
        argv, cwd="/", stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, start_new_session=True,
    ) as proc:  # fmt: skip
        try:
            out, err = proc.communicate(timeout=seconds)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            try:
                out, err = proc.communicate(timeout=CLEANUP_SECONDS)
            except subprocess.TimeoutExpired:  # a descendant in its own session holds the pipes
                out = err = None
            raise subprocess.TimeoutExpired(argv, seconds, out, err) from None
        return proc.returncode, out, err


def _append(path: Path, record: dict[str, Any]) -> None:
    """Append to a private regular file only: no symlink, FIFO, hard link or readable log."""
    data = (json.dumps(record, sort_keys=True) + "\n").encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError("not a single-link regular file")
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise OSError("not private to the runner")
            while data:
                written = os.write(fd, data)
                if written <= 0:
                    raise OSError("no progress writing")
                data = data[written:]
        finally:
            os.close(fd)
    except OSError as exc:
        alert(f"cannot record to {path.name}: {type(exc).__name__}: {exc}")
