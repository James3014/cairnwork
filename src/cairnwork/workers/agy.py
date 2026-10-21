"""A worker that asks an Agy (Antigravity CLI) model to edit the attempt clone.

The model runs in print mode with ``--mode accept-edits --sandbox`` and the
clone as its working directory. What that actually allows was probed on
2026-10-20 (docs/DRILLS.md); it is not a security boundary. Cairnwork never
trusts the worker: it snapshots only the allowed files and rejects any other
change in the clone.
The agy process runs in its own process group, which is killed afterwards. It
gets only the environment it needs: it signs in from files under HOME (checked
2026-10-20), plus locale, config dirs, CA bundles and proxies. Other variables in
Cairnwork's environment do not reach it; proxy URLs may carry credentials, and
agy can read anything under HOME, so this narrows exposure, it is no boundary.
Its output is copied into the log by the parent, capped like a verifier log; a
process it started outside its group is killed if it still carries the attempt
marker, and one that still holds the output afterwards fails the run.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from cairnwork.confined import ATTEMPT_ENV, copy_capped, sweep_marked

ENV_KEEP = (
    "HOME",
    "PATH",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
    "REQUESTS_CA_BUNDLE",
    "ALL_PROXY",
    "all_proxy",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "no_proxy",
)

PROMPT = """You are fixing a small bug in this repository.

Task: {title}

{instructions}

Rules:
- Edit only these files: {allowed}
- Do not create, delete or rename any other file. Do not run commands.
- Make the smallest change that fixes the task; keep the existing style.
- Do not modify tests.
- Work alone and edit the file yourself in this turn: do not start subagents, researchers
  or background tasks (in this mode they never report back).
- The tests that will judge your fix are not in this repository; do not look for them.
  Read the source you need directly.
"""


class AgyWorkerError(Exception):
    pass


@dataclass(frozen=True)
class AgyWorker:
    model: str = "gemini-3.8-flash-high"
    timeout_seconds: int = 900
    agy: str = "agy"

    def run(self, workdir: Path, task) -> None:
        binary = shutil.which(self.agy)
        if binary is None:
            raise AgyWorkerError(f"{self.agy} not found")
        prompt = PROMPT.format(
            title=task.title,
            instructions=task.instructions,
            allowed=", ".join(sorted(task.allowed_files)),
        )
        argv = [
            binary,
            "-p",
            prompt,
            "--model",
            self.model,
            "--mode",
            "accept-edits",
            "--sandbox",
            "--print-timeout",
            f"{self.timeout_seconds}s",
        ]
        log_path = workdir.parent / f"{workdir.name}.agy.log"
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        env = {key: os.environ[key] for key in ENV_KEEP if key in os.environ}
        env[ATTEMPT_ENV] = workdir.name
        log = os.fdopen(fd, "wb")
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            argv,
            cwd=workdir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        copier = threading.Thread(target=copy_capped, args=(process.stdout, log), daemon=True)
        copier.start()
        held = True
        try:
            process.wait(timeout=self.timeout_seconds + 60)
        except subprocess.TimeoutExpired as exc:
            raise AgyWorkerError("agy timed out") from exc
        finally:
            held = _stop(process, copier, workdir.name)
            if not held:  # else the copier still reads: leave both open rather than hang
                process.stdout.close()
                log.close()
        if held:
            raise AgyWorkerError("a process agy started outlived it and holds its output")
        if process.returncode != 0:
            raise AgyWorkerError(f"agy exited {process.returncode}; log {log_path}")


def _stop(process, copier, attempt: str) -> bool:
    """Kill agy's group and anything it started outside it that still carries the
    attempt marker; return True if something still holds agy's output pipe."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass  # the group is already gone
    process.wait()
    sweep_marked(ATTEMPT_ENV, attempt, skip_group=process.pid)
    copier.join(timeout=10)
    return copier.is_alive()
