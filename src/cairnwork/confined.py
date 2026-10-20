"""Run one confined command: own process group, output copied by the parent, capped.

The child never holds the log's file descriptor; after it exits, its process
group is killed and any process that escaped the group but still carries the
run token in its environment is killed too (and reported, so the run fails).
"""

from __future__ import annotations

import os
import resource
import signal
import subprocess
import threading

RUN_TOKEN_VAR = "CAIRNWORK_VERIFIER_RUN"
MAX_LOG_BYTES = 4 << 20
MAX_REPORT_BYTES = 4 << 20


def execute(argv, cwd, env, timeout, log_path) -> tuple[int | None, bool, int]:
    """Run in a new process group; the parent copies output into log_path (the child
    never holds the log's file descriptor); kill the group afterwards."""
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as log:
        process = subprocess.Popen(  # noqa: S603 - argv built from an allow-listed spec, no shell
            argv,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            preexec_fn=_limit_file_size,  # noqa: PLW1509 - only setrlimit, no locks
        )
        copier = threading.Thread(target=_copy_capped, args=(process.stdout, log), daemon=True)
        copier.start()
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        _kill_group(process.pid)
        process.wait()
        survivors = _sweep_survivors(env[RUN_TOKEN_VAR])
        copier.join(timeout=10)  # a descendant that escaped the group may hold the pipe
    return (None if timed_out else process.returncode), timed_out, survivors


def _sweep_survivors(token: str) -> int:
    """Kill processes from this run that escaped the process group (e.g. via setsid).

    They are found by the run token in their environment (ps -E, same user only). A
    process that clears its environment is not found; only a separate uid stops that.
    """
    marker = f"{RUN_TOKEN_VAR}={token}"
    try:
        listing = subprocess.run(  # noqa: S603 - fixed argv
            ["/bin/ps", "-axwwE", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    found = 0
    for line in listing.splitlines():
        pid_text, _, rest = line.strip().partition(" ")
        if marker in rest and pid_text.isdigit() and int(pid_text) != os.getpid():
            try:
                os.kill(int(pid_text), signal.SIGKILL)
                found += 1
            except (ProcessLookupError, PermissionError):
                pass
    return found


def _copy_capped(source, target) -> None:
    """Copy at most MAX_LOG_BYTES + 1 bytes (one over the cap marks the log too big)."""
    written = 0
    for chunk in iter(lambda: source.read(1 << 16), b""):
        keep = chunk[: max(0, MAX_LOG_BYTES + 1 - written)]
        if keep:
            target.write(keep)
            written += len(keep)


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass  # the group is already gone


def _limit_file_size() -> None:
    """In the child: no file it writes (log, report, scratch) may exceed the caps."""
    limit = MAX_LOG_BYTES + MAX_REPORT_BYTES
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
