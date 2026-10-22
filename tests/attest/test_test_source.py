"""The attestation records which hidden-test mode the state was in (Issue #175)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from cairnwork.journal import Journal
from cairnwork_attest.cli import main
from cairnwork_attest.statement import StatementInputError, build_statement, run_test_source
from tests.attest.test_attest_cli import setup_cli_fixture
from tests.attest.test_attest_cli_json import _setup_signed_bundle


def _journal(state: Path, mode: str | None, attempt_id: str = "a1") -> None:
    state.mkdir(parents=True, exist_ok=True)
    journal = Journal(state / "journal.sqlite")
    journal.record("task-1", attempt_id, "DISPATCHED")
    if mode is not None:
        journal.record("task-1", attempt_id, "TEST_SOURCE", mode)


def _statement(tmp_path: Path, repo: Path, mode: str | None, config: str | None = None) -> dict:
    state = tmp_path / "state"
    candidate = setup_cli_fixture(state, repo)
    _journal(state, mode)
    if config is not None:
        (state / "config.json").write_text(config, encoding="utf-8")
    out = tmp_path / "statement.json"
    args = ["statement", "--state", str(state), "--task-id", "task-1", "--attempt-id", "a1"]
    assert main([*args, "--candidate", candidate, "--out", str(out)]) == 0
    return json.loads(out.read_text(encoding="utf-8"))["predicate"]


@pytest.mark.parametrize("mode", ["bundle", "auto"])
def test_the_journaled_mode_is_recorded(tmp_path: Path, repo: Path, mode: str) -> None:
    assert _statement(tmp_path, repo, mode)["test_source"] == mode


def test_config_flipped_after_the_run_does_not_change_the_record(
    tmp_path: Path, repo: Path
) -> None:
    # the run used auto; config.json now says bundle -> the statement must still say auto
    pred = _statement(tmp_path, repo, "auto", json.dumps({"test_source": "bundle"}))
    assert pred["test_source"] == "auto"


def test_config_flipped_to_auto_after_a_bundle_run_still_says_bundle(
    tmp_path: Path, repo: Path
) -> None:
    pred = _statement(tmp_path, repo, "bundle", json.dumps({"test_source": "auto"}))
    assert pred["test_source"] == "bundle"


def test_no_journal_row_is_none_whatever_the_config_says(tmp_path: Path, repo: Path) -> None:
    pred = _statement(tmp_path, repo, None, json.dumps({"test_source": "bundle"}))
    assert pred["test_source"] == "none"


def test_run_test_source_edge_cases(tmp_path: Path) -> None:
    assert run_test_source(tmp_path, "task-1", "a1") == "none"  # no journal at all
    (tmp_path / "journal.sqlite").write_bytes(b"not sqlite")
    assert run_test_source(tmp_path, "task-1", "a1") == "none"
    state = tmp_path / "s"
    _journal(state, "bundle", "a1")
    _journal_extra = Journal(state / "journal.sqlite")
    _journal_extra.record("task-1", "a1", "TEST_SOURCE", "auto")  # contradictory rows
    assert run_test_source(state, "task-1", "a1") == "none"
    assert run_test_source(state, "task-1", "other-attempt") == "none"
    _journal_extra.record("task-1", "a2", "TEST_SOURCE", "yes")  # unknown value
    assert run_test_source(state, "task-1", "a2") == "none"


def test_an_old_journal_is_widened_and_keeps_its_rows(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute("INSERT INTO attempt_journal VALUES (1, 't', 'a', 'DISPATCHED', '', 'now')")
    conn.commit()
    conn.close()
    journal = Journal(path)
    journal.record("t", "a", "TEST_SOURCE", "auto")
    assert [e.phase for e in journal.entries("t")] == ["DISPATCHED", "TEST_SOURCE"]
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        sqlite3.connect(path).execute("DELETE FROM attempt_journal")


OLD_SCHEMA = """
CREATE TABLE attempt_journal (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN
        ('DISPATCHED', 'WORKER_STARTED', 'CANDIDATE', 'VERIFYING', 'NOTARIZING', 'FINISHED')),
    detail TEXT NOT NULL DEFAULT '', at TEXT NOT NULL);
CREATE TRIGGER attempt_journal_no_delete BEFORE DELETE ON attempt_journal
    BEGIN SELECT RAISE(ABORT, 'attempt_journal is append-only'); END;
"""


def test_build_statement_rejects_unknown_test_source(tmp_path: Path, repo: Path) -> None:
    from cairnwork_attest.record import verify_record
    from tests.attest.test_statement import Producer, setup_sample_notary

    db = tmp_path / "notary.sqlite"
    _, verdict = setup_sample_notary(db, repo)
    verified = verify_record(db, "task-1", "a1", expected_candidate=verdict.candidate_commit)
    producer = Producer(name="cairnwork", version="0", git_commit="c" * 40)
    with pytest.raises(StatementInputError):
        build_statement(verified, producer=producer, timestamp="t", test_source="yes")


def test_signed_bundle_with_no_row_says_none_in_verify_json(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    candidate, _, pub, bundle = _setup_signed_bundle(tmp_path, repo, capsys)
    argv = ["verify", str(bundle), "--public-key", str(pub), "--repo", str(repo)]
    assert main([*argv, "--candidate", candidate, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["test_source"] == "none"


def test_verify_json_prints_bundle_mode(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    candidate = setup_cli_fixture(state, repo)
    _journal(state, "bundle")
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
