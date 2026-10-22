"""What the Cairnwork core needs from an OS sandbox, independent of the platform.

The core (src/cairnwork) calls only this interface; each backend module holds the
platform specifics (binary paths, policy language, host directories). A backend
confines untrusted candidate code: default deny, reads only from the system, the
interpreter and the given read roots, writes only to the given write roots, no
network, credential stores denied. Callers must fail closed: when ``select()``
returns None nothing runs, and there is never an unconfined fallback.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class SandboxError(Exception):
    code = "SANDBOX_ERROR"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


@dataclass(frozen=True)
class Policy:
    """The confinement policy text a backend writes to a file before a run."""

    text: str
    # Operator-facing facts about this policy (e.g. a withheld mount). Not part of ``text``, so
    # never hashed: adding a note must not change sandbox_profile_sha256 or any bound digest.
    notes: tuple[str, ...] = ()

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()


class Backend(Protocol):
    name: str

    def available(self) -> bool:
        """True only if the sandbox really confines: probed by a permitted and a denied run."""
        ...

    def policy(
        self,
        read_roots: tuple[Path, ...],
        write_roots: tuple[Path, ...],
        home: Path | None = None,
        mask_home_credentials: bool = True,
    ) -> Policy:
        """Default-deny policy; raises SandboxError for unsafe or too-broad roots.

        ``mask_home_credentials`` masks the launching user's home credentials; pass False when
        the sandbox runs as a different account (the dedicated verifier uid), which cannot reach
        that home and does not need to (the uid boundary denies it). See bwrap.build_policy.
        """
        ...

    def wrap(self, argv: list[str], policy_path: Path) -> list[str]:
        """argv run confined by the policy written at policy_path."""
        ...

    def unconfined(self, argv: list[str]) -> list[str]:
        """argv run through the sandbox binary with an allow-all policy (the verifier
        account's sudoers rule only permits the sandbox binary, not argv itself)."""
        ...
