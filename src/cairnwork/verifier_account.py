"""Run verifiers as a dedicated, unprivileged macOS account (Issue #9).

Inside Seatbelt, a process can still read the arguments of other processes that
run as the same user (sysctl KERN_PROCARGS2); macOS refuses that across users. So
verifiers run as ``cairnwork-verifier`` (created by scripts/setup_verifier_user.sh):

    sudo -n -u cairnwork-verifier -- /usr/bin/sandbox-exec -f <profile>
         /usr/bin/env -i <scrubbed environment> <python> -P -m pytest ...

The sudoers rule lets the owner run sandbox-exec, kill and rm as that account.
sandbox-exec takes any profile, so in effect any process running as the owner can
run code as the account, unsandboxed: the account protects the owner from the
verifier, not the other way round. The account has no privileges of its own.

Every verifier process belongs to that account, so after each run ``kill -9 -1``
as the account ends all of them, including anything that started its own
session; any process found then fails the run. That is only safe if one verifier
runs at a time, so runs hold an exclusive lock, and each locked section starts by
making sure the account runs nothing.
"""

from __future__ import annotations

import fcntl
import os
import pwd
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

SUDO = "/usr/bin/sudo"
DEFAULT_USER = "cairnwork-verifier"
DEFAULT_ROOT = Path("/Users/Shared/cairnwork")


class VerifierAccountError(Exception):
    code = "VERIFIER_ACCOUNT_ERROR"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


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

    def available(self) -> bool:
        """The account is a separate, non-root user, sudo works without a password,
        and the shared Python runs pytest as that user.

        It starts a process as the account, so call it while holding exclusive().
        """
        try:
            uid = pwd.getpwnam(self.user).pw_uid
        except KeyError:
            return False
        if uid in (0, os.getuid()):
            return False  # kill -9 -1 as root or as the owner would be destructive
        probe = self.wrap([self.python, "-P", "-c", "import pytest"], None)
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv
                probe, capture_output=True, timeout=60, check=False, cwd=self.root
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return done.returncode == 0

    def wrap(self, argv: list[str], profile: Path | None, env: dict[str, str] | None = None):
        """sudo -> sandbox-exec -> env -i -> argv (profile None: an allow-all probe)."""
        sandbox = (
            ["/usr/bin/sandbox-exec", "-f", str(profile)]
            if profile is not None
            else ["/usr/bin/sandbox-exec", "-p", "(version 1) (allow default)"]
        )
        environment = ["/usr/bin/env", "-i", *[f"{k}={v}" for k, v in (env or {}).items()]]
        return [SUDO, "-n", "-u", self.user, "--", *sandbox, *environment, *argv]

    def count(self) -> int:
        """How many processes the account is running (-1: cannot tell)."""
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv
                ["/bin/ps", "-x", "-U", self.user, "-o", "pid="],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return -1
        if done.returncode not in (0, 1) or done.stderr.strip():  # 1: nothing matched
            return -1  # e.g. an unknown user, which ps also reports with exit 1
        return len(done.stdout.split())

    def reap(self) -> int:
        """Kill everything the account runs; return how many were running.

        Unknown counts, or a process that outlived the kill, count as one more, so a
        caller never mistakes "cannot tell" for "nothing escaped".
        """
        try:
            before = self.count()
            left = self.kill_all()
        except (OSError, subprocess.SubprocessError):
            return 1
        return max(before, 0) + (1 if before < 0 or left != 0 else 0)

    def kill_all(self, attempts: int = 5) -> int:
        """SIGKILL every process of the account; return how many are left (-1: unknown).

        Repeated a few times: a process that forks while the signal is delivered
        can leave a child behind.
        """
        left = -1
        for _ in range(attempts):
            subprocess.run(  # noqa: S603 - fixed argv
                [SUDO, "-n", "-u", self.user, "--", "/bin/kill", "-9", "-1"],
                capture_output=True,
                timeout=20,
                check=False,
            )
            left = self.count()
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
        real = Path(os.path.realpath(path))
        if self.run_root.resolve() not in real.parents:
            raise VerifierAccountError(f"{real} is outside {self.run_root}")
        subprocess.run(  # noqa: S603 - fixed argv, path checked above
            [SUDO, "-n", "-u", self.user, "--", "/bin/rm", "-rf", "--", str(real)],
            capture_output=True,
            timeout=60,
            check=False,
        )

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        """One verifier at a time: kill_all() would otherwise end another run's processes.

        Not reentrant: flock locks per open file, so a nested call blocks forever.
        """
        fd = os.open(self.root / ".verifier.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)
