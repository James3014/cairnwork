"""Linux bubblewrap (bwrap) confinement for running untrusted candidate code.

The policy is a read-only bind of the whole root plus read-write binds for the write roots,
with a fresh /dev and /proc, every namespace unshared (so no network), all capabilities
dropped and nested user namespaces disabled. Named credential directories under $HOME are
emptied with a tmpfs and credential files are replaced by /dev/null, and root-equivalent
daemon sockets (Docker/Podman/containerd) are replaced by /dev/null — a read-only root bind
does not stop connect() to a pathname socket, which no network namespace covers.

Trade-off (documented in docs/SECURITY.md): until the verifier runs under its own account
(P4) the sandbox runs as the invoking account, so everything that account can read is
readable inside; only the named credential list and daemon sockets are masked. Other users'
0700 home directories are protected by POSIX permissions. This is not complete read isolation,
and the general pathname-socket surface under the whole-root bind closes only with P4.

On Ubuntu 24.04+ an AppArmor rule restricts unprivileged user namespaces by default:
``unshare(CLONE_NEWUSER)`` still succeeds but the new namespace holds no capabilities, so
mounting a fresh ``/proc`` or a bind fails. Lifting ``kernel.apparmor_restrict_unprivileged_userns``
(or, on an end user's machine, loading an AppArmor profile that grants ``userns``) makes
confinement work.

``available()`` probes with a real run: unshare a user and pid namespace with every
capability dropped and nested userns disabled, bind the root read-only and mount a fresh
``/proc``, then run ``true`` — the same hardening the real policy uses, so a build that
rejects one of these flags fails the probe instead of passing it and erroring on the real run.
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
    ".local/share/keyrings",  # gnome-keyring login keyring
    ".terraform.d",
)
_CRED_FILES = (
    ".netrc",
    ".git-credentials",  # plaintext git tokens
    ".npmrc",  # npm publish token
    ".pypirc",  # PyPI token
    ".pgpass",  # PostgreSQL password file
    ".my.cnf",  # MySQL client password
    ".vault-token",  # HashiCorp Vault token
)
# Daemon sockets that, left reachable, grant a candidate the host even with no network
# namespace: a read-only bind does not stop ``connect()`` to a pathname AF_UNIX socket, and
# the Docker/Podman/containerd API is root-equivalent. Replaced with /dev/null so connect
# fails. (This is the known-dangerous set; the general pathname-socket surface under the
# whole-root bind closes only with the P4 dedicated account — see docs/SECURITY.md.)
_DANGEROUS_SOCKETS = (
    "/var/run/docker.sock",
    "/run/docker.sock",
    "/var/run/podman/podman.sock",
    "/run/podman/podman.sock",
    "/run/containerd/containerd.sock",
    "/var/run/containerd/containerd.sock",
    "/run/dbus/system_bus_socket",  # system D-Bus
    "/var/run/dbus/system_bus_socket",
)

# Unshare a user + pid namespace, drop all capabilities, disable nested userns, bind the root
# read-only, mount a fresh /proc, run true. Exit 0 means bwrap can build a confining namespace
# here with the same hardening flags the real policy uses (so a build that rejects one fails
# the probe rather than passing it and then erroring on the real run).
_PROBE = (
    "--unshare-pid",
    "--unshare-user",
    "--cap-drop",
    "ALL",
    "--disable-userns",  # match the real policy: a setuid bwrap that rejects this fails the probe
    "--ro-bind",
    "/",
    "/",
    "--proc",
    "/proc",
    "true",
)


@lru_cache(maxsize=1)
def bwrap_path() -> str | None:
    """Absolute path to bwrap, or None when it is not installed (backend then unavailable).

    A relative result (a relative ``PATH`` entry) is refused: ``wrap`` runs the binary with
    ``cwd`` set to the candidate's checkout, so a relative ``bin/bwrap`` would resolve to a
    program the candidate planted and run everything unconfined. Fail closed instead.
    """
    found = shutil.which("bwrap")
    if found is None or not os.path.isabs(found):
        return None
    return os.path.realpath(found)


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
    read_roots: tuple[Path, ...],
    write_roots: tuple[Path, ...],
    home: Path | None = None,
    mask_home_credentials: bool = True,
) -> Policy:
    """bwrap flags, one argv element per line; later flags override earlier ones.

    ``mask_home_credentials`` masks the credential dirs/files under ``home``. It applies only
    when the sandbox runs as the owner of that home (the launching-account path): under the
    dedicated verifier account (Issue #84) the sandbox runs as a different uid that cannot
    even enter the owner's ``0700`` home, so those tmpfs mounts would fail with EPERM — and
    are unnecessary, because the uid boundary already denies the owner's credentials. The
    narrow-check still uses the real home, and the system socket / ``/run/user`` masks (which
    do not touch the owner's home) are always applied.
    """
    real_home = _safe(home or Path.home())
    # read_roots are validated but never emitted: the whole root is already bound read-only, so
    # (unlike Seatbelt's allow-list) a read root grants no extra scope. Validating still rejects
    # a malformed path early and keeps the two backends' call signatures identical.
    for root in read_roots:
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
    if mask_home_credentials:
        for rel in _CRED_DIRS:
            target = f"{real_home}/{rel}"
            if os.path.lexists(target):
                flags += ["--tmpfs", target]
        for rel in _CRED_FILES:
            target = f"{real_home}/{rel}"
            if os.path.lexists(target):
                flags += ["--ro-bind", "/dev/null", target]
    # Mask root-equivalent daemon sockets (Docker/Podman/containerd): a read-only root bind
    # does not stop connect() to a pathname socket, and no network namespace covers it.
    # Bind over the canonical path (realpath collapses the /var/run -> /run symlink and the
    # duplicate entries to one existing socket; binding /dev/null onto the symlink path itself
    # makes bwrap try to create a mountpoint through it and fail). Only an existing socket is
    # masked: exists() follows the link, so an absent or dangling one adds no flag.
    masked_socks: set[str] = set()
    for sock in _DANGEROUS_SOCKETS:
        real = _safe(sock) if os.path.exists(sock) else None
        if real and real not in masked_socks:
            masked_socks.add(real)
            flags += ["--ro-bind", "/dev/null", real]
    # Empty the whole /run/user runtime tree with a tmpfs: it holds the systemd user bus (a
    # `systemd-run --user` would start a process outside the sandbox), the SSH/keyring agent
    # socket and other session IPC. A tmpfs over the tree covers every uid at once, so it is
    # right whether the candidate runs as the launcher or a different (P4) account uid.
    runtime_dir = _user_runtime_dir()
    if runtime_dir is not None:
        flags += ["--tmpfs", _safe(runtime_dir)]
    return Policy("\n".join(flags) + "\n")


def _user_runtime_dir() -> str | None:
    """`/run/user` (the per-user runtime tree: systemd user buses, session agent sockets) to
    empty with a tmpfs, or None when it is absent. The whole tree is masked, not one UID's
    subdir, so it holds whether the candidate runs as the launching account or (under P4) a
    dedicated verifier account with a different UID — the policy is built before any account
    switch, so keying on the launcher's own uid would mask the wrong subdir."""
    return "/run/user" if os.path.isdir("/run/user") else None


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
        mask_home_credentials: bool = True,
    ) -> Policy:
        return build_policy(read_roots, write_roots, home, mask_home_credentials)

    def wrap(self, argv: list[str], policy_path: Path) -> list[str]:
        return wrap(argv, policy_path)

    def unconfined(self, argv: list[str]) -> list[str]:
        return unconfined(argv)


BACKEND = BwrapBackend()
