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
from dataclasses import dataclass

STATUS_PREFIX = "NEXUS_AGY_DISPATCH "
TUNING_ENV_PREFIX = "NEXUS_AGY_"  # the pool's own tuning; passed through, nothing else is
EXHAUSTED_EXIT = 75
STATUS_TAIL_BYTES = 64 << 10


class AgyPoolExhausted(Exception):
    """The dispatcher reports that no account in the pool has quota left."""


@dataclass(frozen=True)
class Dispatch:
    path: str
    pool_wait_seconds: int = 600
    provider_stall_seconds: int = 900
    stream_no_progress_seconds: int = 900


def argv(dispatch: Dispatch, workdir, model: str, timeout_seconds: int, allowed_files) -> list[str]:
    out = [
        dispatch.path,
        "--cwd",
        str(workdir),
        "--mode",
        "accept-edits",
        "--model",
        model,
        "--timeout",
        str(timeout_seconds),
        "--pool-wait-timeout",
        str(dispatch.pool_wait_seconds),
        "--max-calls",
        "1",
        "--provider-stall-seconds",
        str(dispatch.provider_stall_seconds),
        "--stream-no-progress-seconds",
        str(dispatch.stream_no_progress_seconds),
    ]
    for name in sorted(allowed_files):
        out += ["--write-path", name]
    return out


def tuning_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k.startswith(TUNING_ENV_PREFIX)}


def open_status(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    return os.fdopen(fd, "wb")


def read_status(text: str) -> dict:
    """The last status line, or {} when there is none or it is not a JSON object."""
    for line in reversed(text.splitlines()):
        if line.startswith(STATUS_PREFIX):
            try:
                status = json.loads(line[len(STATUS_PREFIX) :])
            except ValueError:
                return {}
            return status if isinstance(status, dict) else {}
    return {}


def raise_if_exhausted(status_path, returncode: int, log_path) -> None:
    text = status_path.read_bytes()[-STATUS_TAIL_BYTES:].decode("utf-8", "replace")
    if returncode == EXHAUSTED_EXIT and read_status(text).get("status") == "pool_exhausted":
        raise AgyPoolExhausted(
            "POOL_EXHAUSTED: no account in the pool has quota left; "
            f"no other model was tried; log {log_path}"
        )
