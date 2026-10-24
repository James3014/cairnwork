"""Readback-verification helpers for the Notary (split out of notary.py for the 400-line cap).

Pure functions and the constants they need: canonical JSON/digest, the request hash set, and the
checks that decide whether a stored row is a genuine Core-VERIFIED verdict for the right columns.
Behaviour is unchanged — this is a move, not a logic change. Depends only on cairnwork_protocol and
the standard library, so it never imports notary.py (no cycle).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from cairnwork_protocol import (
    acceptance_contract_hash,
    change_set_hash,
    evidence_bundle_hash,
    verification_plan_hash,
)

CORE_ERROR = "CORE_ERROR"

_HASH_KEYS = (
    "acceptance_contract_hash",
    "change_set_hash",
    "verification_plan_hash",
    "evidence_bundle_hash",
)


def _status_and_integrity(response: dict[str, Any]) -> tuple[str, str]:
    verification = response.get("verification")
    if isinstance(verification, dict):
        return str(verification.get("status")), str(verification.get("integrity"))
    error = response.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    return str(code or CORE_ERROR), "UNKNOWN"


def _is_verified(
    http_status: int,
    status: str,
    integrity: str,
    response: dict[str, Any],
    request: dict[str, Any],
    request_hash: str,
) -> bool:
    if http_status != 200 or status != "VERIFIED" or integrity != "VALID":
        return False
    if _digest(request) != request_hash:
        return False
    echoed = response.get("hashes")
    expected = _request_hashes(request)
    if not isinstance(echoed, dict):
        return False
    return all(expected[key] is not None and echoed.get(key) == expected[key] for key in _HASH_KEYS)


def binding_hash(binding: dict[str, Any]) -> str:
    """The requirements_hash an external candidate's contract carries for its binding (#229)."""
    return _digest(binding)


def _columns_match(
    request: dict[str, Any], task_id: str, attempt_id: str, base: str, candidate: str,
    binding: Any = None,
) -> bool:  # fmt: skip
    """A row's key and commit columns must be the ones its stored request was built for. With a
    binding (#229) the target is its executed tree, and it names this base and head (candidate)
    and hashes to the contract's requirements_hash."""
    change_set, contract = request.get("change_set"), request.get("acceptance_contract")
    if not isinstance(change_set, dict) or not isinstance(contract, dict):
        return False
    target = f"git-commit:{candidate}"
    if binding is not None:
        named = isinstance(binding, dict) and [binding.get("base"), binding.get("head")]
        if named != [base, candidate] or contract.get("requirements_hash") != binding_hash(binding):
            return False
        target = f"git-tree:{binding.get('executed_tree')}"
    return (
        contract.get("contract_id") == task_id
        and change_set.get("change_set_id") == f"{task_id}/{attempt_id}"
        and change_set.get("source_revision") == f"git-commit:{base}"
        and change_set.get("target_revision") == target
    )


def _request_hashes(request: dict[str, Any]) -> dict[str, str | None]:
    try:
        return {
            "acceptance_contract_hash": acceptance_contract_hash(request["acceptance_contract"]),
            "change_set_hash": change_set_hash(request["change_set"]),
            "verification_plan_hash": verification_plan_hash(request["verification_plan"]),
            "evidence_bundle_hash": evidence_bundle_hash(request["evidence_bundle"]),
        }
    except (KeyError, TypeError, ValueError):
        return dict.fromkeys(_HASH_KEYS)


def _dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _frozen(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(_dumps(value))


def _digest(value: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_dumps(value).encode("utf-8")).hexdigest()
