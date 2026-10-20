"""Run verifiers as a dedicated, unprivileged macOS account (Issue #9).

Inside Seatbelt, a process can still read the arguments of other processes that
run as the same user (sysctl KERN_PROCARGS2); macOS refuses that across users. So
verifiers run as ``cairnwork-verifier`` (created by scripts/setup_verifier_user.sh):

    sudo -n -u cairnwork-verifier -- /usr/bin/sandbox-exec -f <profile>
         /usr/bin/env -i <scrubbed environment> <python> -P -m pytest ...

The sudoers rule allows only sandbox-exec, kill and rm as that account. Because
every verifier process belongs to that account, cleanup is simple and complete:
after each run ``kill -9 -1`` as the account ends all of them, including anything
that started its own session. That is only safe if one verifier runs at a time,
so runs take an exclusive lock on the account's run root.
"""

from __future__ import annotations

import fcntl
import os
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
        """The account exists, sudo works without a password, and the shared Python runs."""
        probe = self.wrap(["/usr/bin/true"], None)
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv
                probe, capture_output=True, timeout=20, check=False, cwd=self.root
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return done.returncode == 0 and os.access(self.python, os.F_OK)

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
                ["/bin/ps", "-U", self.user, "-o", "pid="],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return -1
        if done.returncode not in (0, 1):  # ps exits 1 when no process matched
            return -1
        return len(done.stdout.split())

    def reap(self) -> int:
        """Kill everything the account runs; return how many were running.

        Unknown counts, or a process that outlived the kill, count as one more, so a
        caller never mistakes "cannot tell" for "nothing escaped".
        """
        before = self.count()
        left = self.kill_all()
        return max(before, 0) + (1 if before < 0 or left != 0 else 0)

    def kill_all(self) -> int:
        """SIGKILL every process of the account; return how many are left (-1: unknown)."""
        subprocess.run(  # noqa: S603 - fixed argv
            [SUDO, "-n", "-u", self.user, "--", "/bin/kill", "-9", "-1"],
            capture_output=True,
            timeout=20,
            check=False,
        )
        return self.count()

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
        """One verifier at a time: kill_all() would otherwise end another run's processes."""
        fd = os.open(self.root / ".verifier.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)
