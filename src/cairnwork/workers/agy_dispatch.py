"""An operator-configured account-pool dispatcher in front of the agy worker.

On a host where several processes share a pool of agy accounts, the operator
names an executable (state ``config.json`` key ``agy_dispatch``) that picks the
account, holds its lease and rotates on quota. It takes the model, mode, working
directory, timeouts and one ``--write-path`` per allowed file, reads the prompt
on stdin and prints one status line on stderr: ``STATUS_PREFIX`` plus a JSON
object. Exit 75 with status ``pool_exhausted`` means no account in the pool has
quota left; that is an environment failure of its own and never a reason to try
another model.

With ``run_as_user`` set (state config ``agy_run_as_user``, Issue #183) the
dispatcher gets ``--run-as-user=<account>`` and runs only the agy process as that
account, through ``sudo`` and the wrapper ``agy-worker-exec`` in a per-run copy of
the clone; account selection and leasing stay with the dispatcher. Whether the run
really ran as that account, and was swept clean, is read from the dispatcher's
worker receipts (agy_receipts.py), never guessed; when the dispatcher reports that
the account, wrapper or copy-back refused, the attempt fails with
``WORKER_ACCOUNT_FAILED``. Agy is never run as the owner instead.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
from dataclasses import dataclass

STATUS_PREFIX = "NEXUS_AGY_DISPATCH "
TUNING_ENV_PREFIX = "NEXUS_AGY_"  # the pool's own tuning; passed through, nothing else is
EXHAUSTED_EXIT = 75
STATUS_LINE_BYTES = 64 << 10
STATUS_LINES_KEPT = 64  # status line plus the worker receipts (three per attempt)
TERM_GRACE_SECONDS = 15
# The worker-account layout of scripts/setup_worker_user.sh and the dispatcher (Nexus-new
# #2884): the owner starts the wrapper as the worker through one sudoers rule; a run lives
# in worker-runs/<32 hex run tag>. A failure kind naming one of these marks means the
# dispatcher could not run agy as the worker account (or copy its edits back).
SUDO = "/usr/bin/sudo"
WORKER_RUNS = "/Users/Shared/cairnwork-worker/worker-runs"
WORKER_WRAPPER = "/Users/Shared/cairnwork-worker/bin/agy-worker-exec"
WORKER_FAILURE_MARKS = ("AGY_WORKER_", "RUN_AS_USER_")
SWEEP_CLEAN = "AGY_WORKER_SWEEP clean"
SWEEP_TIMEOUT_SECONDS = 60
SWEEP_ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"}


class AgyPoolExhausted(Exception):
    """The dispatcher reports that no account in the pool has quota left."""


class AgyWorkerAccountFailed(Exception):
    """The dispatcher could not run agy as the configured worker account (the account,
    the wrapper or the copy-back refused). Agy was not run as the owner instead."""


@dataclass(frozen=True)
class Dispatch:
    path: str
    pool_wait_seconds: int = 600
    provider_stall_seconds: int = 900
    stream_no_progress_seconds: int = 900
    run_as_user: str | None = None  # agy runs as this account (dispatcher --run-as-user)


def valid_run_as_user(config: dict) -> bool:
    """State config ``agy_run_as_user``: unset, or an account name with ``agy_dispatch`` set
    (only the dispatcher can change account)."""
    user = config.get("agy_run_as_user")
    if user is None:
        return True
    name = isinstance(user, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", user)
    return bool(name and config.get("agy_dispatch"))


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
    if dispatch.run_as_user is not None:
        out.append(f"--run-as-user={dispatch.run_as_user}")
    out += [f"--write-path={name}" for name in sorted(allowed_files)]
    return out


def tuning_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k.startswith(TUNING_ENV_PREFIX)}


class StatusReader(threading.Thread):
    """Reads the dispatcher's stderr as it comes and keeps only its own lines: those that
    start with STATUS_PREFIX (at most STATUS_LINES_KEPT of them, each at most
    STATUS_LINE_BYTES), so nothing a chatty child prints reaches the disk or grows in
    memory. Lines the agy child printed are relayed by the dispatcher with a
    ``[provider]`` prefix and so are never kept.

    The final status line is the last kept line; with ``run_as_user`` the worker
    receipts before it are read too (agy_receipts.py). Without it only the final status
    counts, and a forged line could at worst change the failure label (pool exhausted
    or not), never turn a failure into success: the exit code and the snapshot of the
    clone still decide everything else."""

    def __init__(self, stream) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.last = b""
        self.kept: list[bytes] = []
        self.unknown = False  # a read failed: the status may be missing or stale
        self.dropped = False  # more lines than STATUS_LINES_KEPT: the oldest were dropped

    def run(self) -> None:
        failures = 0
        while failures < 3:
            try:
                line = self.stream.readline(STATUS_LINE_BYTES)
            except Exception:  # noqa: BLE001 - keep draining so the dispatcher never blocks
                failures += 1
                self.unknown = True
                continue
            if not line:
                return
            failures = 0  # the limit is on consecutive failures, so draining never stops early
            if line.startswith(STATUS_PREFIX.encode()):
                self.last = line
                self.kept.append(line)
                if len(self.kept) > STATUS_LINES_KEPT:
                    del self.kept[0]
                    self.dropped = True

    def lines(self) -> list[str]:
        """Every kept dispatcher line, in order (the final status line last)."""
        return [line.decode("utf-8", "replace").rstrip("\n") for line in self.kept]

    def describe(self) -> str:
        """For error messages: the dispatcher's final status and failure kind, bounded."""
        if self.unknown:
            return "dispatcher status unknown (stderr read failed)"
        status = self.status()
        shown = {k: str(status.get(k))[:100] for k in ("status", "failure_kind") if k in status}
        return f"dispatcher status {shown}" if shown else "no dispatcher status"

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


def raise_if_worker_refused(status: dict, log_path) -> None:
    """Fail closed on any worker-account failure the dispatcher reports, whatever its exit
    code: a refused account or wrapper never turns into a run as the owner."""
    kind = status.get("failure_kind")
    if isinstance(kind, str) and any(mark in kind for mark in WORKER_FAILURE_MARKS):
        raise AgyWorkerAccountFailed(
            f"WORKER_ACCOUNT_FAILED: {kind[:300]}; agy was not run as the owner; log {log_path}"
        )


def sweep_worker_run(dispatch: Dispatch, run_root: str) -> bool:
    """Kill the run's worker-account processes through the wrapper (the owner cannot
    signal another uid) and remove the run directory a killed dispatcher left behind.
    Used only after the dispatcher was killed and only for a run root its own launch
    receipt named: after a graceful end the dispatcher has removed the directory and the
    wrapper refuses to sweep a missing one (exit 64), so its WORKER_SWEEP receipt is the
    verdict then. True only when the wrapper reports a clean sweep (exit 0, last line
    ``AGY_WORKER_SWEEP clean``) and the directory is then gone."""
    argv = [SUDO, "-n", "-u", str(dispatch.run_as_user), WORKER_WRAPPER, "--sweep", run_root]
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv
            argv,
            cwd="/",
            env=SWEEP_ENV,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=SWEEP_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    lines = done.stdout.strip().splitlines()
    if done.returncode != 0 or not lines or lines[-1] != SWEEP_CLEAN:
        return False
    shutil.rmtree(run_root, ignore_errors=True)
    return not os.path.lexists(run_root)


def ask_to_stop(process) -> None:
    """SIGTERM the dispatcher's group and wait a short while for it to release its lease."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=TERM_GRACE_SECONDS)
    except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
        pass  # the caller kills the group next
