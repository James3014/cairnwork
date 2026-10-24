"""#88: a canary report is evidence only when its content proves the isolation (Codex review of
#239, B5); the sidecar sha256 proves integrity only."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from local_worker_fakes import canary_checks

from experimental.local_worker import canary, corpus_io


# -- B5: a canary report is evidence only with every check and probe passed ------------------------
def _canary(tmp_path, **changes: Any):
    wrapper = tmp_path / "local-worker-tool"
    wrapper.write_bytes(corpus_io.REPO_WRAPPER.read_bytes())
    report = {
        "schema": canary.SCHEMA,
        "checks": canary_checks(),
        "overall": "PASS",
        "mode": "worker",
        "wrapper": str(wrapper),
        "wrapper_sha256": hashlib.sha256(wrapper.read_bytes()).hexdigest(),
        "host": "mini",
        "finished_at": datetime.now(UTC).isoformat(),
        **changes,
    }
    data = json.dumps(report).encode()
    path = tmp_path / "out" / "canary.json"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    Path(str(path) + ".sha256").write_text(hashlib.sha256(data).hexdigest() + "  x\n")
    return lambda: corpus_io.verify_canary(path, tmp_path / "out", wrapper=wrapper, host="mini")


def _check_failed(check: str) -> dict:
    checks = canary_checks()
    checks[check] = dict(checks[check], **{"pass": False})
    return checks


def _failed_probe(check: str) -> dict:
    checks = canary_checks()
    rows = checks[check]["probes"]
    checks[check] = {"pass": True, "probes": [dict(rows[0], **{"pass": False}), *rows[1:]]}
    return checks


def _without(check: str, probe_name: str) -> dict:
    checks = canary_checks()
    rows = [r for r in checks[check]["probes"] if r["probe"] != probe_name]
    checks[check] = {"pass": True, "probes": rows}
    return checks


@pytest.mark.parametrize(
    "changes",
    [
        {"checks": _check_failed("a_read_denied")},  # the repro: overall PASS, this check failed
        {"checks": _check_failed("identity")},
        {"checks": {}},  # the old acceptance fixture: overall PASS and no checks at all
        {"schema": "cairnwork.local_worker.canary/v0"},
        {"checks": _failed_probe("c_network_denied")},  # check says pass, a probe does not
        {"checks": _without("d_launchd_escape_denied", "code escape signal_other_sandbox")},
        {"checks": _without("positive_controls", "pytest runs the probe")},
        {"checks": dict(canary_checks(), extra={"pass": True, "probes": []})},
        {"checks": dict(canary_checks(), identity={"pass": True, "observed": {"user": "james"}})},
    ],
)
def test_canary_evidence_must_be_complete_and_passed(tmp_path, changes):
    verify = _canary(tmp_path, **changes)
    with pytest.raises(corpus_io.InputRefused, match="canary report"):
        verify()


def test_a_complete_passing_canary_is_accepted(tmp_path):
    assert len(_canary(tmp_path)()["report_sha256"]) == 64


def test_too_few_wrapper_refusals_are_not_evidence(tmp_path):
    checks = canary_checks()
    rows = [r for r in checks["b_write_denied"]["probes"] if not r["probe"].startswith("wrapper ")]
    checks["b_write_denied"] = {"pass": True, "probes": rows[:-1] + rows[-1:]}
    with pytest.raises(corpus_io.InputRefused, match="wrapper refusals"):
        _canary(tmp_path, checks=checks)()
