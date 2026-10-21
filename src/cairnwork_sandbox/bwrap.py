"""Linux bubblewrap (bwrap) confinement for running untrusted candidate code.

The policy is a read-only bind of the whole root plus read-write binds for the write roots,
with a fresh /dev and /proc, every namespace unshared (so no network), all capabilities
dropped and nested user namespaces disabled. Named credential directories under $HOME are
emptied with a tmpfs and credential files are replaced by /dev/null.

Trade-off (documented in docs/SECURITY.md): until the verifier runs under its own account
(P4) the sandbox runs as the invoking account, so everything that account can read is
readable inside; only the named credential list is masked. Other users' 0700 home
directories are protected by POSIX permissions. This is not a complete read isolation.

On Ubuntu 24.04+ an AppArmor rule restricts unprivileged user namespaces by default:
``unshare(CLONE_NEWUSER)`` still succeeds but the new namespace holds no capabilities, so
mounting a fresh ``/proc`` or a bind fails. Lifting ``kernel.apparmor_restrict_unprivileged_userns``
(or, on an end user's machine, loading an AppArmor profile that grants ``userns``) makes
confinement work.

``available()`` probes with a real run: unshare a user and pid namespace with every
capability dropped, bind the root read-only and mount a fresh ``/proc``, then run ``true``.
The probe always mounts ``/proc`` on purpose: a probe that only binds the root passes on a
host where a nested ``/proc`` still fails, which would turn a dead backend into a false green.
When bwrap is missing or broken, callers run nothing (there is no unconfined fallback).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from cairnwork_sandbox.backend import Policy, SandboxError

NAME = "bwrap"

_SAFE = re.compile(r"^/[A-Za-z0-9 _./+@~-]*$")
_TOO_BROAD = (
    "/",
    "/home",
    "/tmp",
    "/var",
    "/var/tmp",
    "/usr",
    "/etc",
    "/opt",
    "/srv",
    "/root",
    "/run",
)  # noqa: S108
_CRED_DIRS = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".config",
    ".docker",
    ".kube",
    ".claude",
    ".codex",
    ".gemini",
    ".nexus",
    ".local/state",
)
_CRED_FILES = (".netrc",)

# Unshare a user + pid namespace, drop all capabilities, bind the root read-only, mount a
# fresh /proc, run true. Exit 0 means bwrap can build a confining namespace on this host.
_PROBE = (
    "--unshare-pid",
    "--unshare-user",
    "--cap-drop",
    "ALL",
    "--ro-bind",
    "/",
    "/",
    "--proc",
    "/proc",
    "true",
)


@lru_cache(maxsize=1)
def bwrap_path() -> str | None:
    """Absolute path to bwrap, or None when it is not installed (backend then unavailable)."""
    return shutil.which("bwrap")


@lru_cache(maxsize=1)
def available() -> bool:
    """True only if bwrap can create a confining namespace here, probed with a real run.

    Fails closed on anything unexpected (missing binary, error, timeout): the caller then
    runs nothing rather than falling back to an unconfined run.
    """
    binary = bwrap_path()
    if binary is None:
        return False
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv built from constants
            [binary, *_PROBE], capture_output=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _safe(path: Path | str) -> str:
    """The real path, checked so it cannot add or rewrite a line of the policy."""
    real = os.path.realpath(path)
    if not _SAFE.fullmatch(real):  # match() + '$' would accept a trailing newline
        raise SandboxError(f"path {real!r} has characters not allowed in a policy")
    return real


def _require_narrow(root: str, real_home: str) -> None:
    """A write root may not be '/', a system or temp root, $HOME, or any ancestor of $HOME."""
    if root in _TOO_BROAD or root == real_home or real_home.startswith(root.rstrip("/") + "/"):
        raise SandboxError(f"root {root!r} is too broad for a sandbox policy")


def build_policy(
    read_roots: tuple[Path, ...], write_roots: tuple[Path, ...], home: Path | None = None
) -> Policy:
    """bwrap flags, one argv element per line; later flags override earlier ones."""
    real_home = _safe(home or Path.home())
    for root in read_roots:  # the root is already read-only and readable: validate only
        _safe(root)
    writes = [_safe(root) for root in write_roots]
    for root in writes:
        _require_narrow(root, real_home)
    flags = [
        "--unshare-all",
        # --unshare-all uses --unshare-user-try (soft); --disable-userns needs the hard
        # --unshare-user, so pass it explicitly or bwrap refuses with
        # "--disable-userns requires --unshare-user".
        "--unshare-user",
        "--new-session",
        "--die-with-parent",
        "--cap-drop",
        "ALL",
        "--disable-userns",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
    ]
    for root in writes:
        flags += ["--bind", root, root]
    # Mask only credential paths that exist: a tmpfs or /dev/null bind over a path that is not
    # there fails on top of the read-only root (no mountpoint to create), and a path that does
    # not exist has nothing to leak. The policy is rebuilt per run, so one created later is
    # masked on the next run.
    for rel in _CRED_DIRS:
        target = f"{real_home}/{rel}"
        if os.path.lexists(target):
            flags += ["--tmpfs", target]
    for rel in _CRED_FILES:
        target = f"{real_home}/{rel}"
        if os.path.lexists(target):
            flags += ["--ro-bind", "/dev/null", target]
    return Policy("\n".join(flags) + "\n")


def _binary() -> str:
    binary = bwrap_path()
    if binary is None:
        raise SandboxError("bwrap is not installed")
    return binary


def wrap(argv: list[str], policy_path: Path) -> list[str]:
    binary = _binary()
    flags = [line for line in Path(policy_path).read_text().splitlines() if line]
    return [binary, *flags, "--", *argv]


def unconfined(argv: list[str]) -> list[str]:
    """Through the bwrap binary but not isolated (the verifier account's sudoers rule only
    permits the bwrap binary, not argv itself)."""
    return [_binary(), "--bind", "/", "/", "--dev", "/dev", "--", *argv]


class BwrapBackend:
    """The Backend interface over this module. Methods look module functions up at call time,
    so a test that monkeypatches ``available`` here changes what callers see."""

    name = NAME

    def available(self) -> bool:
        return available()

    def policy(
        self,
        read_roots: tuple[Path, ...],
        write_roots: tuple[Path, ...],
        home: Path | None = None,
    ) -> Policy:
        return build_policy(read_roots, write_roots, home)

    def wrap(self, argv: list[str], policy_path: Path) -> list[str]:
        return wrap(argv, policy_path)

    def unconfined(self, argv: list[str]) -> list[str]:
        return unconfined(argv)


BACKEND = BwrapBackend()
