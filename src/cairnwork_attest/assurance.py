"""Assurance metadata indicating the isolation and execution posture."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from cairnwork_attest.record import AttestError, VerifiedRecord

SandboxBackend = Literal["seatbelt", "bubblewrap", "none", "unknown"]
NetworkMode = Literal["off", "on", "unknown"]
ALLOWED_SANDBOX_BACKENDS: tuple[SandboxBackend, ...] = (
    "seatbelt",
    "bubblewrap",
    "none",
    "unknown",
)
ALLOWED_NETWORK_MODES: tuple[NetworkMode, ...] = ("off", "on", "unknown")


class AssuranceConfigError(AttestError):
    code: str = "ASSURANCE_CONFIG_ERROR"


@dataclass(frozen=True)
class Assurance:
    """Assurance metadata indicating the isolation and execution posture."""

    sandbox_backend: SandboxBackend = "unknown"
    verifier_account_isolated: bool | None = None
    worker_isolated: bool | None = None
    hidden_tests: bool | None = None
    network: NetworkMode = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "sandbox_backend": self.sandbox_backend,
            "verifier_account_isolated": (
                self.verifier_account_isolated
                if self.verifier_account_isolated is not None
                else "unknown"
            ),
            "worker_isolated": (
                self.worker_isolated if self.worker_isolated is not None else "unknown"
            ),
            "hidden_tests": (self.hidden_tests if self.hidden_tests is not None else "unknown"),
            "network": self.network,
        }


def assurance_from_record(verified: VerifiedRecord) -> Assurance:
    """Derive assurance posture from a verified notary record.

    In Phase 1, typed request fields proving verifier_account_isolated,
    sandbox_backend, hidden_tests, worker_isolated, or network do not exist
    in the recorded request payload, so all fields render 'unknown'.
    """
    if not isinstance(verified, VerifiedRecord):
        raise AssuranceConfigError(f"expected VerifiedRecord, got {type(verified).__name__}")
    return Assurance()
