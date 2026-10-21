"""Linux bubblewrap (bwrap) confinement — P2 draft: availability probe only.

This draft lands the availability probe and a CI job (``test-linux-sandbox``) that observes
how bwrap behaves on a GitHub ubuntu-24.04 runner before the policy/wrap implementation and
the witness tests are written. On Ubuntu 24.04+ an AppArmor rule restricts unprivileged user
namespaces by default: ``unshare(CLONE_NEWUSER)`` still succeeds but the new namespace holds
no capabilities, so mounting a fresh ``/proc`` or a bind fails. Lifting
``kernel.apparmor_restrict_unprivileged_userns`` (or, on an end user's machine, loading an
AppArmor profile that grants ``userns`` — the install requirement) makes confinement work.

What this draft does NOT do yet, by design:
- ``policy()`` / ``wrap()`` / ``unconfined()`` are not implemented (they raise), and
- ``platform_backend()`` does not return this backend, so the core still finds no backend on
  Linux and fails closed — nothing runs unconfined. The next PR adds the policy (read-only
  root bind plus named credential masking), ``wrap``, the Linux CI that runs the sandbox
  witnesses, and wires it in.

``available()`` probes with a real run: unshare a user and pid namespace with every
capability dropped, bind the root read-only and mount a fresh ``/proc``, then run ``true``.
The probe always mounts ``/proc`` on purpose — a probe that only binds the root passes on a
host where a nested ``/proc`` still fails, which would turn a dead backend into a false green.
"""

from __future__ import annotations

import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from cairnwork_sandbox.backend import Policy, SandboxError

NAME = "bwrap"

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
        raise SandboxError("bwrap policy() is not implemented yet (P2 draft: probe only)")

    def wrap(self, argv: list[str], policy_path: Path) -> list[str]:
        raise SandboxError("bwrap wrap() is not implemented yet (P2 draft: probe only)")

    def unconfined(self, argv: list[str]) -> list[str]:
        raise SandboxError("bwrap unconfined() is not implemented yet (P2 draft: probe only)")


BACKEND = BwrapBackend()
