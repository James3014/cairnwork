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
import os
import pwd
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from cairnwork_sandbox import Backend, platform_backend

SUDO = "/usr/bin/sudo"
DEFAULT_USER = "cairnwork-verifier"
# The shared root lives where the platform keeps shared service data: /Users/Shared on macOS,
# /var/lib on Linux. scripts/setup_verifier_user.sh (macOS) and setup_verifier_user_linux.sh
# create it there; the account machinery is otherwise identical on both.
DEFAULT_ROOT = Path(
    "/var/lib/cairnwork" if sys.platform.startswith("linux") else "/Users/Shared/cairnwork"
)


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
            os.chmod(policy_file, 0o644)  # seatbelt reads the profile as the account
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

    def count(self) -> int:
        """How many processes the account is running (-1: cannot tell)."""
        try:
            # BSD ps (macOS) needs -x to include processes with no controlling tty; Linux
            # procps selects all of a user's processes with -U alone and rejects -x.
            tty_flag = ["-x"] if sys.platform == "darwin" else []
            done = subprocess.run(  # noqa: S603 - fixed argv
                ["/bin/ps", *tty_flag, "-U", self.user, "-o", "pid="],
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
        """Let the account read, never write, a checkout, whatever the umask was."""
        for top, dirs, files in os.walk(root):
            for name, bits in [(d, 0o050) for d in dirs] + [(f, 0o040) for f in files]:
                path = os.path.join(top, name)
                if not os.path.islink(path):
                    os.chmod(path, os.stat(path).st_mode | bits)
        os.chmod(root, os.stat(root).st_mode | 0o050)

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
