"""Assurance metadata indicating the isolation and execution posture."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from cairnwork_attest.record import AttestError

SandboxBackend = Literal["seatbelt", "bubblewrap", "none", "unknown"]
NetworkMode = Literal["off", "on", "unknown"]
ALLOWED_SANDBOX_BACKENDS = ("seatbelt", "bubblewrap", "none", "unknown")
ALLOWED_NETWORK_MODES = ("off", "on", "unknown")


class AssuranceConfigError(AttestError):
    code: str = "ASSURANCE_CONFIG_ERROR"


@dataclass(frozen=True)
class Assurance:
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
            "hidden_tests": self.hidden_tests if self.hidden_tests is not None else "unknown",
            "network": self.network,
        }


def assurance_from_state(
    state_dir: Path,
    *,
    run_meta: Mapping[str, object] | None = None,
) -> Assurance:
    config_file = state_dir / "config.json"
    if not config_file.is_file():
        raise AssuranceConfigError(
            f"config.json not found in {state_dir}",
            code="CONFIG_NOT_FOUND",
        )
    try:
        content = config_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise AssuranceConfigError(
            f"failed to read config.json: {exc}",
            code="CONFIG_UNREADABLE",
        ) from exc
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise AssuranceConfigError(
            f"config.json is not valid JSON: {exc}",
            code="INVALID_CONFIG_JSON",
        ) from exc
    if not isinstance(parsed, dict):
        raise AssuranceConfigError(
            "config.json must decode to a JSON object",
            code="INVALID_CONFIG_STRUCTURE",
        )
    config = parsed

    verifier_account_isolated: bool | None = None
    if "verifier_account" in config:
        verifier_account_isolated = bool(config.get("verifier_account"))

    sandbox_backend: SandboxBackend = "unknown"
    hidden_tests: bool | None = None
    worker_isolated: bool | None = None
    network: NetworkMode = "unknown"

    if run_meta is not None:
        backend_val = run_meta.get("sandbox_backend")
        if backend_val in ALLOWED_SANDBOX_BACKENDS:
            sandbox_backend = backend_val  # type: ignore[assignment]

        test_commit = run_meta.get("test_commit")
        hidden_tests = bool(test_commit)

        if "worker_isolated" in run_meta and run_meta["worker_isolated"] is not None:
            worker_isolated = bool(run_meta["worker_isolated"])

        net_val = run_meta.get("network")
        if net_val in ALLOWED_NETWORK_MODES:
            network = net_val  # type: ignore[assignment]

    return Assurance(
        sandbox_backend=sandbox_backend,
        verifier_account_isolated=verifier_account_isolated,
        worker_isolated=worker_isolated,
        hidden_tests=hidden_tests,
        network=network,
    )
