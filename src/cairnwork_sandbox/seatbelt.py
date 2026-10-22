"""macOS Seatbelt (sandbox-exec) confinement for running untrusted candidate code.

Rules learned from Nexus-new (docs/LESSONS_FROM_NEXUS.md; knowledge
nexus/research/2026-10-19-layer1-policy-sandbox-L1):

- default deny; reads only from an allow-list (system libraries, the Python
  installation, the checkout); writes only to the checkout and a per-run
  directory; no network; credential stores denied again as the last rules;
- the binary is called by absolute path and probed with a real run;
  when it is missing or broken the caller must fail closed (``available``
  returns False and nothing runs unconfined);
- every path is resolved to its real path (``/tmp`` and ``/var`` are symlinks)
  and checked for characters that could rewrite the policy;
- ``/dev/null`` must be writable or pytest dies with INTERNALERROR.

Known gap: a confined process can still read the arguments and environment of
other processes run by the same user (sysctl KERN_PROCARGS2); no Seatbelt rule
tried blocked it. Running under a separate uid does: see cairnwork/verifier_account.py.

File metadata (stat) is readable everywhere: Python cannot start without it.
That leaks names, sizes and times, not contents. Seatbelt is a macOS tool
Apple documents as legacy; it confines, it is not a portable production sandbox.
"""

from __future__ import annotations

import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path

from cairnwork_sandbox.backend import Policy, SandboxError

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
_SAFE = re.compile(r"^/[A-Za-z0-9 _./+@~-]*$")
SYSTEM_READ_SUBPATHS = ("/usr/lib", "/usr/share", "/System/Library", "/dev")
SYSTEM_READ_LITERALS = ("/", "/private/etc/localtime", "/private/etc/passwd", "/usr/bin/env")
# Where xcodebuild records that the Xcode license was accepted; a full Xcode's tools
# refuse to run without reading it. It holds no user data.
XCODE_LICENSE_PLIST = "/Library/Preferences/com.apple.dt.Xcode.plist"
DEVICE_WRITES = ("/dev/null", "/dev/tty", "/dev/dtracehelper")
CREDENTIAL_HOME_PATHS = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".netrc",
    ".config",
    ".docker",
    ".kube",
    "Library/Keychains",
    "Library/Application Support",
    ".claude",
    ".codex",
    ".gemini",
    ".nexus",
    ".local/state",
)


# Seatbelt calls its policy a profile; the type is the backend-neutral Policy.
Profile = Policy


@lru_cache(maxsize=1)
def available() -> bool:
    """True only if /usr/bin/sandbox-exec runs a permitted command and refuses a denied one."""
    if not os.access(SANDBOX_EXEC, os.X_OK):
        return False
    allowed = _probe("(version 1) (allow default)")
    denied = _probe("(version 1) (deny default)")
    return allowed == 0 and denied not in (None, 0)


def _probe(policy: str) -> int | None:
    """Exit code of /usr/bin/true under policy; a working sandbox must refuse when denied."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv
            [SANDBOX_EXEC, "-p", policy, "/usr/bin/true"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.returncode


def sbpl_path(path: Path | str) -> str:
    """The real path, checked so it cannot break out of an SBPL string."""
    real = os.path.realpath(path)
    if not _SAFE.fullmatch(real):  # match() + '$' would accept a trailing newline
        raise SandboxError(f"path {real!r} has characters not allowed in a profile")
    return real


# Only the two standard places: the Command Line Tools, or an Xcode app bundle.
_DEVELOPER_DIR = re.compile(
    r"/Library/Developer/CommandLineTools|/Applications/Xcode[A-Za-z0-9._-]*\.app/Contents/Developer"
)


@lru_cache(maxsize=1)
def developer_dirs() -> tuple[str, ...]:
    """The active developer directory (read-only): /usr/bin/git and friends are xcrun
    shims that load their tools from it, so without it no test can run git. For a full
    Xcode the whole .app is readable: its tools load frameworks from SharedFrameworks."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv
            ["/usr/bin/xcode-select", "-p"], capture_output=True, text=True, timeout=10,
            check=False,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return ()
    real = os.path.realpath(done.stdout.strip()) if done.returncode == 0 else ""
    if not _DEVELOPER_DIR.fullmatch(real) or not os.path.isdir(real):
        return ()
    return (real.removesuffix("/Contents/Developer"),)


def build_profile(
    read_roots: tuple[Path, ...], write_roots: tuple[Path, ...], home: Path | None = None
) -> Profile:
    """Default-deny profile: reads from system + read_roots, writes to write_roots only."""
    real_home = sbpl_path(home or Path.home())
    for root in (*read_roots, *write_roots):
        _require_narrow(sbpl_path(root), real_home)
    reads = [f'(subpath "{sbpl_path(p)}")' for p in (*read_roots, *write_roots)]
    writes = [f'(subpath "{sbpl_path(p)}")' for p in write_roots]
    denied = [f'(subpath "{real_home}/{rel}")' for rel in CREDENTIAL_HOME_PATHS]
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-fork)",
        "(allow process-exec)",
        "(allow signal (target same-sandbox))",
        "(allow sysctl-read)",
        "(allow file-read-metadata)",
        "(allow file-read*",
        *[f'  (subpath "{p}")' for p in SYSTEM_READ_SUBPATHS],
        *[f'  (literal "{p}")' for p in SYSTEM_READ_LITERALS],
        *[f'  (subpath "{p}")' for p in developer_dirs()],
        *[f'  (literal "{XCODE_LICENSE_PLIST}")' for p in developer_dirs() if p.endswith(".app")][
            :1
        ],
        *[f"  {rule}" for rule in reads],
        ")",
        "(allow file-write*",
        *[f'  (literal "{p}")' for p in DEVICE_WRITES],
        *[f"  {rule}" for rule in writes],
        ")",
        "(deny network*)",
        "(deny file-read* file-write*",
        *[f"  {rule}" for rule in denied],
        ")",
    ]
    return Profile("\n".join(lines) + "\n")


_TOO_BROAD = (
    "/",
    "/Users",
    "/Users/Shared",
    "/private",
    "/private/var",
    "/private/var/folders",
    "/var",
    "/var/folders",
    "/private/tmp",
    "/tmp",
    "/opt",
    "/opt/homebrew",
    "/usr",
    "/usr/local",
)


def _require_narrow(root: str, real_home: str) -> None:
    """A root may not be '/', $HOME, a temp root, or any ancestor of $HOME."""
    if root in _TOO_BROAD or root == real_home or real_home.startswith(root.rstrip("/") + "/"):
        raise SandboxError(f"root {root!r} is too broad for a sandbox profile")


def wrap(argv: list[str], profile_path: Path) -> list[str]:
    return [SANDBOX_EXEC, "-f", str(profile_path), *argv]


ALLOW_ALL = "(version 1) (allow default)"


class SeatbeltBackend:
    """The Backend interface over this module. Methods look the module functions up at call
    time, so a test that monkeypatches e.g. ``available`` here changes what callers see."""

    name = "seatbelt"

    def available(self) -> bool:
        return available()

    def policy(
        self,
        read_roots: tuple[Path, ...],
        write_roots: tuple[Path, ...],
        home: Path | None = None,
        mask_home_credentials: bool = True,
    ) -> Policy:
        # Seatbelt is default-deny (allow-list): the owner's home is unreadable whether or not
        # the explicit credential denies are present, and they mount nothing, so there is no
        # EPERM under the dedicated account. The flag is accepted for the Backend protocol and
        # intentionally ignored — macOS behaviour is unchanged on either path.
        return build_profile(read_roots, write_roots, home)

    def wrap(self, argv: list[str], policy_path: Path) -> list[str]:
        return wrap(argv, policy_path)

    def unconfined(self, argv: list[str]) -> list[str]:
        return [SANDBOX_EXEC, "-p", ALLOW_ALL, *argv]


BACKEND = SeatbeltBackend()
