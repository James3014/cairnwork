"""Run verifiers as a dedicated, unprivileged account (Issue #9 on macOS, #84 on Linux).

A sandbox alone is not complete read isolation: inside Seatbelt a process can still read
the arguments of other processes that run as the same user (sysctl KERN_PROCARGS2), and
under the Linux bwrap backend the whole root is bound read-only, so anything the running
account can read is readable. Running verifiers as a different, unprivileged account closes
both: the readable set becomes that account's, which owns nothing. The account is created by
scripts/setup_verifier_user.sh (macOS) or scripts/setup_verifier_user_linux.sh:

    sudo -n -u cairnwork-verifier -- <sandbox> /usr/bin/env -i <env> <python> -P -m pytest ...

where ``<sandbox>`` is sandbox-exec on macOS and bwrap on Linux (both from the backend).
The sudoers rule lets the owner run the sandbox binary, kill and rm as that account. The
sandbox binary takes any policy, so in effect any process running as the owner can run code
as the account: the account protects the owner from the verifier, not the other way round.
The account has no privileges of its own.

Every verifier process belongs to that account, so after each run ``kill -9 -1``
as the account ends all of them, including anything that started its own
session; any process found then fails the run. That is only safe if one verifier
runs at a time, so runs hold an exclusive lock, and each locked section starts by
making sure the account runs nothing.
"""

from __future__ import annotations

import fcntl
import math
import os
import pwd
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from cairnwork import confined, verifier_escape
from cairnwork_sandbox import Backend, platform_backend

SUDO = "/usr/bin/sudo"
DEFAULT_USER = "cairnwork-verifier"
# The shared root lives where the platform keeps shared service data: /Users/Shared on macOS,
# /var/lib on Linux. scripts/setup_verifier_user.sh (macOS) and setup_verifier_user_linux.sh
# create it there; the account machinery is otherwise identical on both.
DEFAULT_ROOT = Path(
    "/var/lib/cairnwork" if sys.platform.startswith("linux") else "/Users/Shared/cairnwork"
)


# A waiter gives up after this long instead of queueing behind a stuck holder. run_verifiers
# holds the lock across every spec (each bounded by its timeout, 600 s by default), so it does
# not use this fixed floor alone: lock_wait_seconds() sizes the wait from the spec count.
# Waiters poll, so there is no FIFO order: under steady contention one can starve until its
# own timeout, which is the bound (Issue #140: a cross-process ticket queue would add stale
# tickets and crashed-holder recovery for a starvation that already fails closed).
LOCK_TIMEOUT_SECONDS = 900.0
_LOCK_MARGIN_SECONDS = 300.0
# The derived wait never exceeds this (4 h, about 23 specs at the 600 s default): a huge spec
# count or timeout must not turn a waiter into one that queues for days (Issue #140).
LOCK_WAIT_CAP_SECONDS = 14400.0
_LOCK_POLL_SECONDS = 0.05
# One budget for every process-list probe and kill of a single reap or idle check (Issue #114);
# past it the answer is "survivor / unknown", never "clean".
PROBE_DEADLINE_SECONDS = 120.0


def _remaining(deadline: float | None) -> float:
    return confined.PS_TIMEOUT_SECONDS if deadline is None else deadline - time.monotonic()


def lock_wait_seconds(spec_count: int, spec_timeout: float) -> float:
    """How long a waiter should queue for a holder like itself: every spec at its full timeout,
    plus a margin, never below LOCK_TIMEOUT_SECONDS nor above LOCK_WAIT_CAP_SECONDS (both read
    at call time; the cap wins). ``spec_timeout`` must be a finite number >= 0: a negative or nan
    one would otherwise vanish into the floor and hide a caller bug (ValueError)."""
    if not math.isfinite(spec_timeout) or spec_timeout < 0:
        raise ValueError(f"spec timeout must be a finite number >= 0, got {spec_timeout!r}")
    wanted = max(spec_count, 0) * spec_timeout + _LOCK_MARGIN_SECONDS
    return min(LOCK_WAIT_CAP_SECONDS, max(LOCK_TIMEOUT_SECONDS, wanted))


class VerifierAccountError(Exception):
    code = "VERIFIER_ACCOUNT_ERROR"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


def _backend() -> Backend:
    backend = platform_backend()
    if backend is None:
        raise VerifierAccountError("no sandbox backend on this platform")
    return backend


@dataclass(frozen=True)
class VerifierAccount:
    user: str = DEFAULT_USER
    root: Path = DEFAULT_ROOT

    @property
    def run_root(self) -> Path:
        return self.root / "runs"

    @property
    def python(self) -> str:
        return str(self.root / "venv" / "bin" / "python")

    def available(self, python: str | None = None) -> bool:
        """The account is a separate, non-root user, sudo works without a password, and a
        **confined** run as that user can import pytest. The probe uses the real sandbox policy
        (credential masks skipped, as on the account path), not an allow-all run, so "usable"
        means confinement actually works — a policy that fails to mount (e.g. a tmpfs under a
        home the account cannot enter) reports unusable instead of a false healthy.

        It starts a process as the account, so call it while holding exclusive().
        """
        from cairnwork import pytest_config  # local import keeps the module import-cycle free

        try:
            uid = pwd.getpwnam(self.user).pw_uid
        except KeyError:
            return False
        if uid in (0, os.getuid()):
            return False  # kill -9 -1 as root or as the owner would be destructive
        py = python or self.python
        policy_file: Path | None = None
        try:
            policy = _backend().policy(
                read_roots=pytest_config.interpreter_roots(py),
                write_roots=(),
                mask_home_credentials=False,
            )
            fd, name = tempfile.mkstemp(dir=self.root, suffix=".policy")
            policy_file = Path(name)
            os.write(fd, policy.text.encode())
            os.close(fd)
            os.chmod(policy_file, 0o640)  # group-readable: seatbelt reads it as the account
            probe = self.wrap([py, "-P", "-c", "import pytest"], policy_file)
            done = subprocess.run(  # noqa: S603 - fixed argv
                probe, capture_output=True, timeout=60, check=False, cwd=self.root
            )
        except (OSError, subprocess.SubprocessError, VerifierAccountError):
            return False
        finally:
            if policy_file is not None:
                policy_file.unlink(missing_ok=True)
        return done.returncode == 0

    def wrap(self, argv: list[str], profile: Path | None, env: dict[str, str] | None = None):
        """sudo -> sandbox-exec -> env -i -> argv (profile None: an allow-all probe)."""
        environment = ["/usr/bin/env", "-i", *[f"{k}={v}" for k, v in (env or {}).items()]]
        inner = [*environment, *argv]
        backend = _backend()
        jailed = backend.wrap(inner, profile) if profile is not None else backend.unconfined(inner)
        return [SUDO, "-n", "-u", self.user, "--", *jailed]

    def _list(self, deadline: float | None, attempts: int) -> list[tuple[int, int]] | None:
        """(pid, ppid) for each process the account runs; None if it cannot be determined within the
        deadline. ppid is the kernel's parent pid, which the process cannot forge. Retries like
        count(): slow under load is not "cannot tell" (Issue #114)."""
        # BSD ps (macOS) needs -x to include processes with no controlling tty; Linux
        # procps selects all of a user's processes with -U alone and rejects -x.
        tty_flag = ["-x"] if sys.platform == "darwin" else []
        for _ in range(attempts):
            budget = _remaining(deadline)
            if budget <= 0:
                break
            try:
                done = subprocess.run(  # noqa: S603 - fixed argv
                    ["/bin/ps", *tty_flag, "-U", self.user, "-o", "pid=,ppid="],
                    capture_output=True,
                    text=True,
                    timeout=min(budget, confined.PS_TIMEOUT_SECONDS),
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if done.returncode in (0, 1) and not done.stderr.strip():  # 1: nothing matched
                procs: list[tuple[int, int]] = []
                for line in done.stdout.split("\n"):
                    parts = line.split()
                    if len(parts) == 2 and all(p.isdigit() for p in parts):
                        procs.append((int(parts[0]), int(parts[1])))
                return procs
        return None  # e.g. an unknown user, which ps also reports with exit 1

    def count(self, deadline: float | None = None, attempts: int = confined.PS_ATTEMPTS) -> int:
        """How many processes the account is running, launchd system daemons included (-1: cannot
        tell, or ``deadline`` passed). ``_escaped_count`` decides whether a run left anything.

        Retry (``attempts`` > 1) only after a kill: asking again while a survivor is still alive
        gives it time to undo the run's traces (Issue #114).
        """
        procs = self._list(deadline, attempts)
        return -1 if procs is None else len(procs)

    def _real_paths(self, pids: list[int]) -> dict[int, str] | None:
        """Map each pid to its real executable path (proc_pidpath), run AS THE ACCOUNT so it may
        query its own processes. None if the helper could not run (treated as "cannot tell")."""
        if not pids:
            return {}
        argv = self.wrap(
            [self.python, "-P", "-c", verifier_escape.PIDPATH_HELPER, *map(str, pids)], None
        )
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv, pids from ps
                argv, capture_output=True, text=True, timeout=30, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if done.returncode != 0:
            return None
        paths: dict[int, str] = {}
        for line in done.stdout.split("\n"):
            if "\t" in line:
                pid_s, path = line.split("\t", 1)
                if pid_s.isdigit():
                    paths[int(pid_s)] = path
        return paths

    def _escaped_count(
        self, deadline: float | None = None, attempts: int = confined.PS_ATTEMPTS
    ) -> int:
        """Processes the account runs that are NOT a launchd system daemon (-1: cannot tell).
        A daemon is exempt only when its real executable is allow-listed, its parent is launchd
        (ppid 1) and it is Apple-signed; anything else — a candidate's leftover process, however it
        named itself — is counted. If the list or the real paths cannot be read, the count is
        unknown (fail closed), never zero."""
        procs = self._list(deadline, attempts)
        if procs is None:
            return -1
        # Only a launchd child (ppid 1) can be an exempt daemon, and proc_pidpath is macOS-only, so
        # resolve real paths (a separate probe, as the account) ONLY for those pids, and not at all
        # off Darwin or when none have ppid 1 — every other process simply counts.
        launchd = [pid for pid, ppid in procs if ppid == 1]
        if not launchd or sys.platform != "darwin":
            return len(procs)
        real = self._real_paths(launchd)
        if real is None:
            return -1
        exempt = sum(1 for pid in launchd if verifier_escape.is_exempt(real.get(pid), 1))
        return len(procs) - exempt

    def reap(self) -> int:
        """Kill everything the account runs; return how many escaped processes were running.

        The process list is read once, *before* the kill, with no retry: asking again while a
        survivor is alive would give it time to undo the run's traces and exit before it is
        counted (``kill -9 -1`` cannot report what it hit: it signals itself too, and ps is not
        allowed inside the account's sandbox). Retrying is only done after a kill. An unknown
        count, an expired probe deadline, or a process that outlived the kill count as one
        more, so a caller never mistakes "cannot tell" for "nothing escaped".
        """
        deadline = time.monotonic() + PROBE_DEADLINE_SECONDS
        try:
            before = self._escaped_count(deadline, attempts=1)
            left = self._kill_until_clean(5, deadline)
        except (OSError, subprocess.SubprocessError):
            return 1
        return max(before, 0) + (1 if before < 0 or left != 0 else 0)

    def kill_all(self, attempts: int = 5) -> int:
        """SIGKILL every process of the account; return how many ESCAPED processes are left (-1:
        unknown). A launchd system daemon is exempt from the count (``_escaped_count``), so one that
        launchd keeps restarting does not block termination."""
        return self._kill_until_clean(attempts, time.monotonic() + PROBE_DEADLINE_SECONDS)

    def _kill_until_clean(self, attempts: int, deadline: float) -> int:
        """How many processes are left after the kills (-1: unknown).

        Repeated a few times: a process that forks while the signal is delivered can leave a
        child behind. Everything shares ``deadline``; when it passes the answer is unknown.
        """
        left = -1
        for _ in range(attempts):
            budget = _remaining(deadline)
            if budget <= 0:
                return -1
            try:
                subprocess.run(  # noqa: S603 - fixed argv
                    [SUDO, "-n", "-u", self.user, "--", "/bin/kill", "-9", "-1"],
                    capture_output=True,
                    timeout=min(budget, confined.PS_TIMEOUT_SECONDS),
                    check=False,
                )
            except subprocess.TimeoutExpired:
                continue  # a loaded host: ask again
            left = self._escaped_count(deadline)
            if left == 0:
                break
        return left

    def require_idle(self) -> None:
        """Before a locked section: end anything a dead or failed run left behind."""
        left = self.kill_all()
        if left != 0:
            raise VerifierAccountError(f"account {self.user} still runs processes ({left})")

    def remove(self, path: Path) -> None:
        """Remove files the account created (the owner cannot), only inside its run root."""
        if os.path.islink(path):  # never follow a name the account may have swapped
            return
        real = Path(os.path.realpath(path))
        if self.run_root.resolve() not in real.parents:
            raise VerifierAccountError(f"{real} is outside {self.run_root}")
        # The account may have left directories without permissions: rm -rf alone cannot
        # enter them. chmod runs as the account (allowed only through sandbox-exec).
        for argv in (
            _backend().unconfined(["/bin/chmod", "-R", "u+rwx", "--", str(real)]),
            ["/bin/rm", "-rf", "--", str(real)],
        ):
            subprocess.run(  # noqa: S603 - fixed argv, path checked above
                [SUDO, "-n", "-u", self.user, "--", *argv],
                capture_output=True,
                timeout=60,
                check=False,
            )

    @staticmethod
    def share_run(private: Path, ini: Path, run_dir: Path, home: Path, tmp: Path) -> None:
        """Let the account (group cairnwork) enter the run dir and read the ini, and
        write only its run/ scratch. The log stays owner-only."""
        os.chmod(private, 0o750)
        os.chmod(ini, 0o640)
        os.chmod(run_dir, 0o3770)  # sticky: the account cannot swap home/ or tmp/ away
        for directory in (home, tmp):
            os.chmod(directory, 0o2770)

    @staticmethod
    def grant_read(root: Path) -> None:
        """Let the account read and traverse, never write, a checkout, whatever the umask was.

        Read is granted to *other*, not only the shared group (Issue #91). The checkout cannot be
        relied on to carry the shared ``cairnwork`` group: its parent ``work`` inherits the group
        and the setgid bit from the run root, but the per-run ``chmod`` that opens ``work`` to the
        group strips setgid on Linux whenever the owner process is not an effective member of the
        group (a service whose session predates ``usermod -aG``), so git clones the checkout under
        the owner's own primary group instead. Group bits then never reach the account. With the
        default umask the clone happens to be other-readable and the run works; under a strict umask
        (077) it is not, and the account cannot enter the checkout — the run fails. Granting *other*
        read/traverse makes the account's access umask- and group-independent; it is no wider than
        the default-umask clone already is, and is scoped to this throwaway checkout of the
        code under test, never the owner's home or the run scratch."""
        for top, dirs, files in os.walk(root):
            for name, bits in [(d, 0o055) for d in dirs] + [(f, 0o044) for f in files]:
                path = os.path.join(top, name)
                if not os.path.islink(path):
                    os.chmod(path, os.stat(path).st_mode | bits)
        os.chmod(root, os.stat(root).st_mode | 0o055)

    @contextmanager
    def exclusive(self, timeout: float | None = None) -> Iterator[None]:
        """One verifier at a time: kill_all() would otherwise end another run's processes.

        Waits at most ``timeout`` seconds, then raises RESOURCE_LEASE_TIMEOUT so a stuck but
        live holder turns into a recorded failure, not an endless queue (a dead holder's lock
        is released by the kernel). Not reentrant: flock locks per open file, so a nested call
        waits until the timeout. ``timeout`` must be a finite number >= 0 (default
        LOCK_TIMEOUT_SECONDS, read at call time); anything else raises ValueError.
        """
        if timeout is None:
            timeout = LOCK_TIMEOUT_SECONDS
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError(f"lock timeout must be a finite number >= 0, got {timeout!r}")
        fd = os.open(self.root / ".verifier.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise VerifierAccountError(
                            f"RESOURCE_LEASE_TIMEOUT: account {self.user} busy for {timeout:g}s"
                        ) from None
                    time.sleep(min(_LOCK_POLL_SECONDS, remaining))
            yield
        finally:
            os.close(fd)
