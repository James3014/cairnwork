"""Tests for cairnwork_attest.assurance."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cairnwork_attest.assurance import Assurance, AssuranceConfigError, assurance_from_state


def test_assurance_from_state_reads_verifier_account_true(tmp_path: Path) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"verifier_account": True}), encoding="utf-8")

    assurance = assurance_from_state(tmp_path)
    assert assurance.verifier_account_isolated is True


def test_assurance_from_state_reads_verifier_account_false(tmp_path: Path) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"verifier_account": False}), encoding="utf-8")

    assurance = assurance_from_state(tmp_path)
    assert assurance.verifier_account_isolated is False


def test_assurance_from_state_reads_verifier_account_missing(tmp_path: Path) -> None:
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"model": "test-model"}), encoding="utf-8")

    assurance = assurance_from_state(tmp_path)
    assert assurance.verifier_account_isolated is None


def test_assurance_from_state_no_config_file(tmp_path: Path) -> None:
    with pytest.raises(AssuranceConfigError) as exc_info:
        assurance_from_state(tmp_path)
    assert exc_info.value.code == "CONFIG_NOT_FOUND"


def test_assurance_never_returns_true_without_evidence(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    assurance = assurance_from_state(tmp_path)
    assert assurance.verifier_account_isolated is not True
    assert assurance.worker_isolated is not True
    assert assurance.hidden_tests is not True
    assert assurance.sandbox_backend == "unknown"
    assert assurance.network == "unknown"


def test_assurance_sandbox_backend_allowed_and_unknown(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    for backend in ("seatbelt", "bubblewrap", "none", "unknown"):
        assurance = assurance_from_state(tmp_path, run_meta={"sandbox_backend": backend})
        assert assurance.sandbox_backend == backend

    invalid = assurance_from_state(tmp_path, run_meta={"sandbox_backend": "docker"})
    assert invalid.sandbox_backend == "unknown"


def test_assurance_hidden_tests_from_test_commit(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    present = assurance_from_state(tmp_path, run_meta={"test_commit": "a" * 40})
    assert present.hidden_tests is True

    empty = assurance_from_state(tmp_path, run_meta={"test_commit": ""})
    assert empty.hidden_tests is False

    omitted = assurance_from_state(tmp_path, run_meta={})
    assert omitted.hidden_tests is False

    no_meta = assurance_from_state(tmp_path, run_meta=None)
    assert no_meta.hidden_tests is None


def test_assurance_worker_isolated_not_assumed_false(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    explicit_true = assurance_from_state(tmp_path, run_meta={"worker_isolated": True})
    assert explicit_true.worker_isolated is True

    explicit_false = assurance_from_state(tmp_path, run_meta={"worker_isolated": False})
    assert explicit_false.worker_isolated is False

    not_specified = assurance_from_state(tmp_path, run_meta={})
    assert not_specified.worker_isolated is None

    no_meta = assurance_from_state(tmp_path, run_meta=None)
    assert no_meta.worker_isolated is None


def test_assurance_network_mode(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    for mode in ("off", "on", "unknown"):
        assurance = assurance_from_state(tmp_path, run_meta={"network": mode})
        assert assurance.network == mode

    unrecognised = assurance_from_state(tmp_path, run_meta={"network": "bridged"})
    assert unrecognised.network == "unknown"

    no_meta = assurance_from_state(tmp_path, run_meta=None)
    assert no_meta.network == "unknown"


def test_assurance_from_state_malformed_json(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text("not-json{", encoding="utf-8")
    with pytest.raises(AssuranceConfigError) as exc_info:
        assurance_from_state(tmp_path)
    assert exc_info.value.code == "INVALID_CONFIG_JSON"


def test_assurance_from_state_non_object_json(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(AssuranceConfigError) as exc_info:
        assurance_from_state(tmp_path)
    assert exc_info.value.code == "INVALID_CONFIG_STRUCTURE"


def test_to_dict_renders_unknown() -> None:
    default_assurance = Assurance()
    rendered = default_assurance.to_dict()

    assert rendered["sandbox_backend"] == "unknown"
    assert rendered["verifier_account_isolated"] == "unknown"
    assert rendered["worker_isolated"] == "unknown"
    assert rendered["hidden_tests"] == "unknown"
    assert rendered["network"] == "unknown"

    configured = Assurance(
        sandbox_backend="seatbelt",
        verifier_account_isolated=True,
        worker_isolated=False,
        hidden_tests=None,
        network="off",
    )
    rendered_configured = configured.to_dict()
    assert rendered_configured["sandbox_backend"] == "seatbelt"
    assert rendered_configured["verifier_account_isolated"] is True
    assert rendered_configured["worker_isolated"] is False
    assert rendered_configured["hidden_tests"] == "unknown"
    assert rendered_configured["network"] == "off"
