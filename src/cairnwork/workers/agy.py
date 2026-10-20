"""A worker that asks an Agy (Antigravity CLI) model to edit the attempt clone.

The model runs in print mode with ``--mode accept-edits --sandbox`` and the
clone as its working directory. What that actually allows was probed on
2026-10-20 (docs/DRILLS.md); it is not a security boundary. Cairnwork never
trusts the worker: it snapshots only the allowed files and rejects any other
change in the clone.
The agy process runs in its own process group, which is killed afterwards.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

ATTEMPT_ENV = "CAIRNWORK_ATTEMPT"  # lets recovery find this worker if Cairnwork dies

PROMPT = """You are fixing a small bug in this repository.

Task: {title}

{instructions}

Rules:
- Edit only these files: {allowed}
- Do not create, delete or rename any other file. Do not run commands.
- Make the smallest change that fixes the task; keep the existing style.
- Do not modify tests.
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
        with os.fdopen(fd, "wb") as log:
            process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                argv,
                cwd=workdir,
                env={**os.environ, ATTEMPT_ENV: workdir.name},
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                process.wait(timeout=self.timeout_seconds + 60)
            except subprocess.TimeoutExpired as exc:
                raise AgyWorkerError("agy timed out") from exc
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                process.wait()
        if process.returncode != 0:
            raise AgyWorkerError(f"agy exited {process.returncode}; log {log_path}")
