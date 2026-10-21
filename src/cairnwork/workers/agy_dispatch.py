"""An operator-configured account-pool dispatcher in front of the agy worker.

On a host where several processes share a pool of agy accounts, the operator
names an executable (state ``config.json`` key ``agy_dispatch``) that picks the
account, holds its lease and rotates on quota. It takes the model, mode, working
directory, timeouts and one ``--write-path`` per allowed file, reads the prompt
on stdin and prints one status line on stderr: ``STATUS_PREFIX`` plus a JSON
object. Exit 75 with status ``pool_exhausted`` means no account in the pool has
quota left; that is an environment failure of its own and never a reason to try
another model.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from dataclasses import dataclass

STATUS_PREFIX = "NEXUS_AGY_DISPATCH "
TUNING_ENV_PREFIX = "NEXUS_AGY_"  # the pool's own tuning; passed through, nothing else is
EXHAUSTED_EXIT = 75
STATUS_LINE_BYTES = 64 << 10
TERM_GRACE_SECONDS = 15


class AgyPoolExhausted(Exception):
    """The dispatcher reports that no account in the pool has quota left."""


@dataclass(frozen=True)
class Dispatch:
    path: str
    pool_wait_seconds: int = 600
    provider_stall_seconds: int = 900
    stream_no_progress_seconds: int = 900


def argv(dispatch: Dispatch, workdir, model: str, timeout_seconds: int, allowed_files) -> list[str]:
    """Every value is attached with '=' so a value that starts with '-' is never an option."""
    out = [
        dispatch.path,
        f"--cwd={workdir}",
        "--mode=accept-edits",
        f"--model={model}",
        f"--timeout={timeout_seconds}",
        f"--pool-wait-timeout={dispatch.pool_wait_seconds}",
        "--max-calls=1",
        f"--provider-stall-seconds={dispatch.provider_stall_seconds}",
        f"--stream-no-progress-seconds={dispatch.stream_no_progress_seconds}",
    ]
    out += [f"--write-path={name}" for name in sorted(allowed_files)]
    return out


def tuning_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k.startswith(TUNING_ENV_PREFIX)}


class StatusReader(threading.Thread):
    """Reads the dispatcher's stderr as it comes and keeps only the last status line (at
    most STATUS_LINE_BYTES of it), so nothing a chatty child prints reaches the disk or
    grows in memory, and the status line is found however much diagnostics follow it.

    Only the final status line counts. The dispatcher prints it last on every exit-75 path
    and the agy child's stderr cannot follow it; a forged earlier or later line could at
    worst change the failure label (pool exhausted or not), never turn a failure into
    success: the exit code and the snapshot of the clone still decide everything else."""

    def __init__(self, stream) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.last = b""

    def run(self) -> None:
        for line in iter(lambda: self.stream.readline(STATUS_LINE_BYTES), b""):
            if line.startswith(STATUS_PREFIX.encode()):
                self.last = line

    def status(self) -> dict:
        return read_status(self.last.decode("utf-8", "replace"))


def read_status(text: str) -> dict:
    """The JSON object after the status prefix, or {} when there is none."""
    if not text.startswith(STATUS_PREFIX):
        return {}
    try:
        status = json.loads(text[len(STATUS_PREFIX) :])
    except ValueError:
        return {}
    return status if isinstance(status, dict) else {}


def raise_if_exhausted(status: dict, returncode: int, log_path) -> None:
    if returncode == EXHAUSTED_EXIT and status.get("status") == "pool_exhausted":
        raise AgyPoolExhausted(
            "POOL_EXHAUSTED: no account in the pool has quota left; "
            f"no other model was tried; log {log_path}"
        )


def ask_to_stop(process) -> None:
    """SIGTERM the dispatcher's group and wait a short while for it to release its lease."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=TERM_GRACE_SECONDS)
    except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
        pass  # the caller kills the group next
