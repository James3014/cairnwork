"""Assurance metadata indicating the isolation and execution posture."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from cairnwork_attest.record import AttestError, VerdictRecord

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
    """Assurance metadata indicating the isolation and execution posture.

    Values derived from run_meta are caller-asserted and rendered under a
    separate key ``asserted_by_caller: true`` in ``to_dict()`` whenever any
    run_meta value was used.
    """

    sandbox_backend: SandboxBackend = "unknown"
    verifier_account_isolated: bool | None = None
    worker_isolated: bool | None = None
    hidden_tests: bool | None = None
    network: NetworkMode = "unknown"
    asserted_by_caller: bool = False

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
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
        if self.asserted_by_caller:
            result["asserted_by_caller"] = True
        return result


def _extract_verifier_account_isolated(request: Mapping[str, Any]) -> bool | None:
    if not isinstance(request, Mapping):
        return None
    if "verifier_account" in request and request["verifier_account"] is not None:
        return bool(request["verifier_account"])
    if "sandbox" in request and isinstance(request["sandbox"], bool):
        return request["sandbox"]

    bundle = request.get("evidence_bundle")
    if isinstance(bundle, Mapping):
        observations = bundle.get("observations")
        if isinstance(observations, list):
            for obs in observations:
                if isinstance(obs, Mapping):
                    if "verifier_account" in obs and obs["verifier_account"] is not None:
                        return bool(obs["verifier_account"])
                    if "sandbox" in obs and isinstance(obs["sandbox"], bool):
                        return obs["sandbox"]
                    if (
                        "verifier_account_isolated" in obs
                        and obs["verifier_account_isolated"] is not None
                    ):
                        return bool(obs["verifier_account_isolated"])
    return None


def _is_test_file(path: str) -> bool:
    cleaned = path.strip()
    return (
        cleaned.startswith("tests/")
        or cleaned.startswith("test/")
        or cleaned.endswith("_test.py")
        or cleaned.endswith("test.py")
    )


def _extract_referenced_test_files(request: Mapping[str, Any]) -> set[str]:
    files: set[str] = set()
    for section_key in ("acceptance_contract", "verification_plan"):
        section = request.get(section_key)
        if not isinstance(section, Mapping):
            continue
        for key in ("test_files", "tests", "allowed_paths"):
            val = section.get(key)
            if isinstance(val, (list, tuple)):
                for item in val:
                    if isinstance(item, str) and (
                        _is_test_file(item) or key in ("test_files", "tests")
                    ):
                        files.add(item)
        for key in ("commands", "verification"):
            val = section.get(key)
            if isinstance(val, (list, tuple)):
                for cmd in val:
                    if isinstance(cmd, (list, tuple)):
                        for arg in cmd:
                            if isinstance(arg, str) and _is_test_file(arg):
                                files.add(arg)
                    elif isinstance(cmd, str):
                        for part in cmd.split():
                            if _is_test_file(part):
                                files.add(part)
    return files


def _is_test_applied(request: Mapping[str, Any], test_files: set[str]) -> bool:
    bundle = request.get("evidence_bundle")
    if not isinstance(bundle, Mapping):
        return False
    observations = bundle.get("observations")
    if not isinstance(observations, list) or not observations:
        return False
    for obs in observations:
        if not isinstance(obs, Mapping):
            continue
        obs_strings = [
            str(obs.get("verifier_id", "")),
            str(obs.get("artifact_id", "")),
            str(obs.get("test_file", "")),
        ]
        for tf in test_files:
            if any(tf in s for s in obs_strings):
                return True
    return False


def assurance_from_record(
    record: VerdictRecord,
    *,
    run_meta: Mapping[str, object] | None = None,
) -> Assurance:
    """Derive assurance posture from the notary record and optional caller-asserted run_meta.

    verifier_account_isolated is derived strictly from evidence in the bound request
    (either an explicit verifier_account or sandbox marker in the request or in any
    evidence_bundle observation).

    hidden_tests is True only when run_meta supplies test_commit and the request's
    acceptance contract or verification plan references a test file that the record
    shows was applied; otherwise None.

    sandbox_backend and network come only from run_meta, defaulting to 'unknown'.
    Values supplied via run_meta are caller-asserted; when any run_meta value is used,
    the resulting Assurance sets asserted_by_caller=True, which renders
    'asserted_by_caller: true' in to_dict().
    """
    verifier_account_isolated = _extract_verifier_account_isolated(record.request)

    sandbox_backend: SandboxBackend = "unknown"
    network: NetworkMode = "unknown"
    worker_isolated: bool | None = None
    hidden_tests: bool | None = None
    asserted_by_caller = False

    if run_meta is not None:
        if "sandbox_backend" in run_meta:
            backend_val = run_meta.get("sandbox_backend")
            if backend_val in ALLOWED_SANDBOX_BACKENDS:
                sandbox_backend = backend_val  # type: ignore[assignment]
            asserted_by_caller = True

        if "network" in run_meta:
            net_val = run_meta.get("network")
            if net_val in ALLOWED_NETWORK_MODES:
                network = net_val  # type: ignore[assignment]
            asserted_by_caller = True

        if "worker_isolated" in run_meta and run_meta["worker_isolated"] is not None:
            worker_isolated = bool(run_meta["worker_isolated"])
            asserted_by_caller = True

        test_commit = run_meta.get("test_commit")
        if test_commit and isinstance(test_commit, str):
            asserted_by_caller = True
            ref_files = _extract_referenced_test_files(record.request)
            if ref_files and _is_test_applied(record.request, ref_files):
                hidden_tests = True
        elif "test_commit" in run_meta:
            asserted_by_caller = True

    return Assurance(
        sandbox_backend=sandbox_backend,
        verifier_account_isolated=verifier_account_isolated,
        worker_isolated=worker_isolated,
        hidden_tests=hidden_tests,
        network=network,
        asserted_by_caller=asserted_by_caller,
    )


# Backward compatibility alias
assurance_from_state = assurance_from_record
