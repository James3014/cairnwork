"""The attestation records which hidden-test mode the state was in (Issue #175)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cairnwork_attest.cli import main
from cairnwork_attest.statement import StatementInputError, build_statement, state_test_source
from tests.attest.test_attest_cli import setup_cli_fixture
from tests.attest.test_attest_cli_json import _setup_signed_bundle


def _statement(tmp_path: Path, repo: Path, config: str | None) -> dict:
    state = tmp_path / "state"
    candidate = setup_cli_fixture(state, repo)
    if config is not None:
        (state / "config.json").write_text(config, encoding="utf-8")
    out = tmp_path / "statement.json"
    args = ["statement", "--state", str(state), "--task-id", "task-1", "--attempt-id", "a1"]
    assert main([*args, "--candidate", candidate, "--out", str(out)]) == 0
    return json.loads(out.read_text(encoding="utf-8"))["predicate"]


def test_bundle_state_is_recorded_as_bundle(tmp_path: Path, repo: Path) -> None:
    pred = _statement(tmp_path, repo, json.dumps({"test_source": "bundle"}))
    assert pred["test_source"] == "bundle"


@pytest.mark.parametrize(
    "config", [None, "{}", '{"test_source": "auto"}', '{"test_source": "Bundle"}', "not json", "[]"]
)
def test_anything_but_exact_bundle_is_recorded_as_auto(
    tmp_path: Path, repo: Path, config: str | None
) -> None:
    assert _statement(tmp_path, repo, config)["test_source"] == "auto"


def test_state_test_source_reads_config(tmp_path: Path) -> None:
    assert state_test_source(tmp_path) == "auto"
    (tmp_path / "config.json").write_text('{"test_source": "bundle"}')
    assert state_test_source(tmp_path) == "bundle"


def test_build_statement_rejects_unknown_test_source(tmp_path: Path, repo: Path) -> None:
    from cairnwork_attest.record import verify_record
    from tests.attest.test_statement import Producer, setup_sample_notary

    db = tmp_path / "notary.sqlite"
    _, verdict = setup_sample_notary(db, repo)
    verified = verify_record(db, "task-1", "a1", expected_candidate=verdict.candidate_commit)
    producer = Producer(name="cairnwork", version="0", git_commit="c" * 40)
    with pytest.raises(StatementInputError):
        build_statement(verified, producer=producer, timestamp="t", test_source="yes")


def test_signed_bundle_carries_test_source_and_verify_json_prints_it(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # fixture signs with no config.json -> auto; the verify output must say so, not null
    candidate, _, pub, bundle = _setup_signed_bundle(tmp_path, repo, capsys)
    argv = ["verify", str(bundle), "--public-key", str(pub), "--repo", str(repo)]
    assert main([*argv, "--candidate", candidate, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["test_source"] == "auto"


def test_verify_json_prints_bundle_mode(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    candidate = setup_cli_fixture(state, repo)
    (state / "config.json").write_text('{"test_source": "bundle"}')
    main(["keygen", "--state", str(state)])
    pem = (
        "-----BEGIN PUBLIC KEY-----"
        + capsys.readouterr().out.split("-----BEGIN PUBLIC KEY-----")[1]
    )
    (tmp_path / "pub.pem").write_text(pem)
    stmt = ["statement", "--state", str(state), "--task-id", "task-1", "--attempt-id", "a1"]
    assert main([*stmt, "--candidate", candidate, "--out", str(tmp_path / "b.json"), "--sign"]) == 0
    capsys.readouterr()
    ver = ["verify", str(tmp_path / "b.json"), "--public-key", str(tmp_path / "pub.pem")]
    assert main([*ver, "--repo", str(repo), "--candidate", candidate, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["test_source"] == "bundle"
