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
import sys
import threading

RUN_TOKEN_VAR = "CAIRNWORK_VERIFIER_RUN"
ATTEMPT_ENV = "CAIRNWORK_ATTEMPT"  # marks every process of an attempt, for recovery
MAX_LOG_BYTES = 4 << 20
MAX_REPORT_BYTES = 4 << 20


def execute(argv, cwd, env, timeout, log_path, reap=None) -> tuple[int | None, bool, int]:
    """Run in a new process group; the parent copies output into log_path (the child
    never holds the log's file descriptor); kill the group afterwards."""
    token = env.get(RUN_TOKEN_VAR)
    if not token:
        raise ValueError(f"{RUN_TOKEN_VAR} must be set so escaped processes can be found")
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
        # Another account's processes: the owner cannot signal them; reap() kills them
        # all and says how many were running (the main one, if it timed out, included).
        try:
            escaped = reap() if reap is not None else 0
        finally:
            _kill_group(process.pid)
            process.wait()
        survivors = _sweep_survivors(token, process.pid) + escaped
        copier.join(timeout=10)  # a descendant that escaped the group may hold the pipe
        process.stdout.close()
    return (None if timed_out else process.returncode), timed_out, survivors


def _sweep_survivors(token: str, group: int) -> int:
    found = sweep_marked(RUN_TOKEN_VAR, token, skip_group=group)
    return 1 if found == SWEEP_UNAVAILABLE else found  # unknown counts as a survivor


SWEEP_UNAVAILABLE = -1


def sweep_marked(var: str, value: str, *, skip_group: int = -1) -> int:
    """Kill processes whose environment holds var=value (same user only); return how many.

    Used for processes that escaped a run's process group (e.g. via setsid) and for a
    dead run's workers. Members of skip_group were just sent SIGKILL and are not
    counted. A process that clears its environment is not found; only a separate
    uid stops that. Returns SWEEP_UNAVAILABLE if the process environments cannot be
    read (callers must treat that as "may still be running", never as "none").
    """
    marker = f"{var}={value}"
    processes = _processes_with_environment()
    if processes is None:
        return SWEEP_UNAVAILABLE
    found = 0
    for pid, pgid, environment in processes:
        if pgid == skip_group or pid == os.getpid() or marker not in environment:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            found += 1
        except (ProcessLookupError, PermissionError):
            pass
    return found


def _processes_with_environment():
    """[(pid, pgid, set of 'NAME=value' tokens)] or None if it cannot be read."""
    if sys.platform.startswith("linux"):
        return _linux_processes()
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv
            ["/bin/ps", "-axwwE", "-o", "pid=,pgid=,command="],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0 or not done.stdout.strip():
        return None
    rows = []
    for line in done.stdout.splitlines():
        fields = line.split(None, 2)
        if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
            rows.append((int(fields[0]), int(fields[1]), set(fields[2].split())))
    return rows


def _linux_processes():
    rows = []
    try:
        entries = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return None
    for name in entries:
        try:
            with open(f"/proc/{name}/environ", "rb") as handle:
                environment = handle.read().decode("utf-8", "replace").split("\0")
            pgid = os.getpgid(int(name))
        except (OSError, ProcessLookupError):
            continue  # gone, or another user's process
        rows.append((int(name), pgid, set(environment)))
    return rows


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
