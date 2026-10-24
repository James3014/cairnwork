"""What a canary report must contain before an episode may cite it (#88; Codex review of #239, B5).

``overall`` alone is not evidence: a report could say PASS with a failed or missing check. A
report is accepted only with the canary's schema, exactly the six checks canary.run_canary
writes, each passed, each probe row passed, every required probe present (one per read, write,
network and escape target, plus the leak, leftover and positive-control rows, and at least six
wrapper refusals for reads and for writes), and the worker identity. The sidecar sha256 the
corpus also checks proves integrity only (the bytes did not change after writing), not who
wrote them: this module is what checks the content.
"""

from __future__ import annotations

from typing import Any

from experimental.local_worker import probe
from experimental.local_worker.canary import SCHEMA
from experimental.local_worker.tools import WORKER_USER

# canary.build_fixture's target names (a direct-mode canary run in the tests checks they match)
READ_TARGETS = ("private_bundle", "other_attempt", "harness", "owner_home")
WRITE_TARGETS = (
    "run_root_outside_checkout",
    "other_attempt",
    "runs_dir",
    "private_bundle",
    "private_tmp",
    "users_shared",
    "checkout_git_hook",
    "checkout_git_hook_upper",
    "checkout_git_config_via_link",
    "git_query_dir",
)
REQUIRED_PROBES = {
    "a_read_denied": {f"code read {n}" for n in READ_TARGETS} | {"no secret in any tool output"},
    "b_write_denied": {f"code write {n}" for n in WRITE_TARGETS}
    | {"no write target exists afterwards"},
    "c_network_denied": {f"code net {n}" for n in probe.NET_TARGETS},
    "d_launchd_escape_denied": {f"code escape {n}" for n in probe.ESCAPE_TARGETS},
    "positive_controls": {"read a checkout file", "write a checkout file", "pytest runs the probe"},
}
MIN_WRAPPER_ROWS = {"a_read_denied": 6, "b_write_denied": 6}
CHECKS = (*REQUIRED_PROBES, "identity")
OVERALL = {"worker": "PASS", "direct": "SEATBELT_ONLY_PASS"}


class CanaryEvidenceError(ValueError):
    """The report does not prove the isolation it claims."""


def _probes(name: str, check: Any) -> None:
    rows = check.get("probes")
    if not isinstance(rows, list) or not rows:
        raise CanaryEvidenceError(f"check {name} has no probe rows")
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("probe"), str):
            raise CanaryEvidenceError(f"check {name} has a malformed probe row")
        if row.get("pass") is not True:
            raise CanaryEvidenceError(f"check {name}: probe {row['probe']!r} did not pass")
    names = {row["probe"] for row in rows}
    missing = sorted(REQUIRED_PROBES[name] - names)
    if missing:
        raise CanaryEvidenceError(f"check {name} lacks probes {missing}")
    wrapper_rows = sum(n.startswith("wrapper ") for n in names)
    if wrapper_rows < MIN_WRAPPER_ROWS.get(name, 0):
        raise CanaryEvidenceError(f"check {name} has {wrapper_rows} wrapper refusals")


def check_evidence(report: Any, mode: str = "worker", user: str = WORKER_USER) -> None:
    """Raise CanaryEvidenceError unless ``report`` is a complete passing canary of ``mode``."""
    if not isinstance(report, dict) or report.get("schema") != SCHEMA:
        raise CanaryEvidenceError(f"canary report schema is not {SCHEMA!r}")
    if report.get("mode") != mode or report.get("overall") != OVERALL[mode]:
        raise CanaryEvidenceError(f"canary report is not an overall {OVERALL[mode]} in {mode} mode")
    checks = report.get("checks")
    if not isinstance(checks, dict) or set(checks) != set(CHECKS):
        raise CanaryEvidenceError(f"canary checks must be exactly {sorted(CHECKS)}")
    for name in CHECKS:
        check = checks[name]
        if not isinstance(check, dict) or check.get("pass") is not True:
            raise CanaryEvidenceError(f"canary check {name} did not pass")
        if name != "identity":
            _probes(name, check)
    observed = checks["identity"].get("observed")
    if not isinstance(observed, dict) or observed.get("user") != user:
        raise CanaryEvidenceError(f"canary identity is not {user!r}")
