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
With an account-pool dispatcher configured (agy_dispatch.py) the worker runs that
instead of agy, prompt on stdin, same environment plus the pool's tuning variables.
With ``agy_run_as_user`` set as well, the dispatcher runs agy as that worker account,
and the attempt succeeds only if the dispatcher's worker receipts prove it ran as that
account and was swept clean; a run left behind by a killed dispatcher is swept through
the wrapper (agy_receipts.py).
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
from cairnwork.workers import agy_dispatch, agy_receipts

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
- Work alone and edit the allowed files yourself in this turn: do not start subagents,
  researchers or background tasks (in this mode they never report back).
{hidden_tests}"""

# Only true for a task with deferred tests (test_commit): otherwise the judging tests are
# the repository's own, which the worker may and should read.
HIDDEN_TESTS = """- The tests that will judge your fix are not in this repository; do not look for
  them. Read the source you need directly.
"""


class AgyWorkerError(Exception):
    pass


@dataclass(frozen=True)
class AgyWorker:
    model: str = "gemini-3.8-flash-high"
    timeout_seconds: int = 900
    agy: str = "agy"
    dispatch: agy_dispatch.Dispatch | None = None

    def run(self, workdir: Path, task) -> None:
        dispatch = self.dispatch
        binary = shutil.which(self.agy) if dispatch is None else None
        if dispatch is None:
            if binary is None:
                raise AgyWorkerError(f"{self.agy} not found")
        elif not (os.path.isfile(dispatch.path) and os.access(dispatch.path, os.X_OK)):
            raise AgyWorkerError(f"{dispatch.path} not found or not executable")
        prompt = PROMPT.format(
            title=task.title,
            instructions=task.instructions,
            allowed=", ".join(sorted(task.allowed_files)),
            hidden_tests=HIDDEN_TESTS if getattr(task, "test_commit", "") else "",
        )
        if dispatch is not None:
            argv = agy_dispatch.argv(
                dispatch, workdir, self.model, self.timeout_seconds, task.allowed_files
            )
        else:
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
        if dispatch is not None:
            env.update(agy_dispatch.tuning_env())
        log = os.fdopen(fd, "wb")
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            argv,
            cwd=workdir,
            env=env,
            stdin=subprocess.DEVNULL if dispatch is None else subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if dispatch is None else subprocess.PIPE,
            start_new_session=True,
        )
        reader = None
        if dispatch is not None:
            reader = agy_dispatch.StatusReader(process.stderr)
            reader.start()
            threading.Thread(target=_feed, args=(process, prompt), daemon=True).start()
        copier = threading.Thread(target=copy_capped, args=(process.stdout, log), daemon=True)
        copier.start()
        self._await(process, copier, reader, workdir.name, log, log_path)

    def _await(self, process, copier, reader, attempt: str, log, log_path) -> None:
        dispatch = self.dispatch
        timed_out = None
        try:
            process.wait(timeout=self.timeout_seconds + 60)
        except subprocess.TimeoutExpired as exc:
            timed_out = exc
            if dispatch is not None:
                agy_dispatch.ask_to_stop(process)  # lets it release its lease and sweep itself
        finally:
            held = _stop(process, copier, attempt, reader)
            if not held:  # else the copier still reads: leave both open rather than hang
                process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
                log.close()
        completion = None
        if dispatch is not None and dispatch.run_as_user is not None:
            completion = _settle_worker_run(dispatch, reader, process, held, timed_out, log_path)
        if timed_out is not None:
            raise AgyWorkerError("agy timed out") from timed_out
        if held:
            raise AgyWorkerError("a process agy started outlived it and holds its output")
        if reader is not None:
            agy_dispatch.raise_if_exhausted(reader.status(), process.returncode, log_path)
            agy_dispatch.raise_if_worker_refused(reader.status(), log_path)
        if process.returncode != 0:
            why = f"; {reader.describe()}" if reader is not None else ""
            raise AgyWorkerError(f"agy exited {process.returncode}{why}; log {log_path}")
        if completion is not None:
            raise agy_dispatch.AgyWorkerAccountFailed(f"{completion}; log {log_path}")


def _settle_worker_run(dispatch, reader, process, held, timed_out, log_path) -> str | None:
    """Judge a run under agy_run_as_user from the dispatcher's receipts and write its
    record. After an abnormal end (a signal, held output, no final status) runs the
    receipts do not show swept are swept through the wrapper first. Raises for an unswept
    run and for an attempt the receipts do not show isolated and clean; returns the
    completion problem of an exit-0 run (raised after the exit-code checks, so a
    dispatcher-reported failure keeps its own label)."""
    known = not (held or reader.unknown or reader.dropped)
    receipts = agy_receipts.parse(reader.lines(), dispatch.run_as_user, known=known)
    code = process.returncode
    killed = timed_out is not None or code < 0
    unswept = []
    if killed or held or receipts.final is None:
        unswept = agy_receipts.sweep_after_kill(dispatch, receipts, killed=killed)
    found = agy_receipts.problems(receipts)
    completion = agy_receipts.completion_problem(receipts) if code == 0 else None
    ended = ["TIMED_OUT"] * (timed_out is not None) + ["HELD"] * held
    extra = [completion] if completion else []
    agy_receipts.write_record(log_path, receipts, unswept + ended + found + extra)
    if unswept:
        raise AgyWorkerError(
            f"WORKER_SWEEP_UNCLEAN: worker-account processes may survive: {unswept}; log {log_path}"
        )
    if found and not ended:
        raise agy_dispatch.AgyWorkerAccountFailed(f"{'; '.join(found)}; log {log_path}")
    return completion


def _feed(process, prompt: str) -> None:
    try:
        process.stdin.write(prompt.encode())
    except OSError:
        pass  # the dispatcher exited without reading it; its exit code says what happened
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass


def _stop(process, copier, attempt: str, reader=None) -> bool:
    """Kill agy's group and anything it started outside it that still carries the
    attempt marker; return True if something still holds agy's output pipe."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass  # the group is already gone
    process.wait()
    sweep_marked(ATTEMPT_ENV, attempt, skip_group=process.pid)
    copier.join(timeout=10)
    if reader is not None:
        reader.join(timeout=10)
    return copier.is_alive() or (reader is not None and reader.is_alive())
