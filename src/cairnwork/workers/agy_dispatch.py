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
the clone; account selection and leasing stay with the dispatcher. When the
dispatcher reports that the account, wrapper or copy-back refused, the attempt
fails with ``WORKER_ACCOUNT_FAILED``; agy is never run as the owner instead. The
owner cannot signal the worker account's processes, so after the dispatcher is
killed its run is swept through the wrapper's ``--sweep`` mode.
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

from cairnwork.confined import PS_TIMEOUT_SECONDS

STATUS_PREFIX = "NEXUS_AGY_DISPATCH "
TUNING_ENV_PREFIX = "NEXUS_AGY_"  # the pool's own tuning; passed through, nothing else is
EXHAUSTED_EXIT = 75
STATUS_LINE_BYTES = 64 << 10
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
        self.unknown = False  # a read failed: the status may be missing or stale

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


def worker_run_roots(dispatcher_pid: int) -> set[str] | None:
    """Run directories of the worker launches the dispatcher has running, read from the
    process list: ``sudo -n -u <account> <wrapper> <run_root> ...`` with the dispatcher as
    parent. Read while the dispatcher is alive (its launches are re-parented once it
    dies); None when the process list could not be read, which must count as unknown."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv
            ["/bin/ps", "-axwwo", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            timeout=PS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0 or not done.stdout.strip():
        return None
    roots = set()
    for line in done.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) != 3 or fields[1] != str(dispatcher_pid):
            continue
        args = fields[2].split()
        if WORKER_WRAPPER not in args:
            continue
        at = args.index(WORKER_WRAPPER)
        if at >= 2 and args[at - 2] == "-u" and at + 1 < len(args) and _is_run_root(args[at + 1]):
            roots.add(args[at + 1])
    return roots


def _is_run_root(path: str) -> bool:
    return re.fullmatch(re.escape(WORKER_RUNS) + r"/[0-9a-f]{32}", path) is not None


def sweep_worker_run(dispatch: Dispatch, run_root: str) -> bool:
    """Kill the run's worker-account processes through the wrapper (the owner cannot
    signal another uid) and remove the run directory the killed dispatcher left behind.
    True when nothing of the run is left: the dispatcher already removed the directory
    (it does so only after its own clean sweep), or the wrapper reports a clean sweep and
    the directory is gone. The wrapper's own report is the only evidence accepted."""
    if not os.path.lexists(run_root):
        return True
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
