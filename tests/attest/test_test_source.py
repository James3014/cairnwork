"""The attestation records which hidden-test mode the state was in (Issue #175)."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from cairnwork.journal import Journal
from cairnwork_attest.cli import main
from cairnwork_attest.record import AttestError
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


def test_run_test_source_none_only_without_a_row(tmp_path: Path) -> None:
    assert run_test_source(tmp_path, "task-1", "a1") == "none"  # no journal at all
    state = tmp_path / "s"
    _journal(state, None, "a1")
    assert run_test_source(state, "task-1", "a1") == "none"  # journal, no TEST_SOURCE row
    Journal(state / "journal.sqlite").record("task-1", "a1", "TEST_SOURCE", "bundle")
    assert run_test_source(state, "task-1", "other-attempt") == "none"


def test_run_test_source_fails_closed_on_an_unreadable_journal(tmp_path: Path) -> None:
    (tmp_path / "journal.sqlite").write_bytes(b"not sqlite")
    with pytest.raises(AttestError):
        run_test_source(tmp_path, "task-1", "a1")


def test_run_test_source_fails_closed_on_a_locked_journal(tmp_path: Path) -> None:
    state = tmp_path / "s"
    _journal(state, "bundle", "a1")
    holder = sqlite3.connect(state / "journal.sqlite", isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(AttestError):
            run_test_source(state, "task-1", "a1")
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_run_test_source_fails_closed_on_a_journal_without_the_table(tmp_path: Path) -> None:
    sqlite3.connect(tmp_path / "journal.sqlite").close()
    with pytest.raises(AttestError):
        run_test_source(tmp_path, "task-1", "a1")


def test_run_test_source_fails_closed_on_contradictory_or_unknown_rows(tmp_path: Path) -> None:
    state = tmp_path / "s"
    _journal(state, "bundle", "a1")
    extra = Journal(state / "journal.sqlite")
    extra.record("task-1", "a1", "TEST_SOURCE", "auto")  # contradictory rows
    with pytest.raises(AttestError):
        run_test_source(state, "task-1", "a1")
    extra.record("task-1", "a2", "TEST_SOURCE", "yes")  # unknown value
    with pytest.raises(AttestError):
        run_test_source(state, "task-1", "a2")


def test_statement_cli_signs_nothing_when_the_journal_is_unreadable(
    tmp_path: Path, repo: Path
) -> None:
    state = tmp_path / "state"
    candidate = setup_cli_fixture(state, repo)
    (state / "journal.sqlite").write_bytes(b"not sqlite")
    out = tmp_path / "statement.json"
    args = ["statement", "--state", str(state), "--task-id", "task-1", "--attempt-id", "a1"]
    assert main([*args, "--candidate", candidate, "--out", str(out)]) == 2
    assert not out.exists()


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


def _old_journal(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute("INSERT INTO attempt_journal VALUES (1, 't', 'a', 'DISPATCHED', '', 'now')")
    conn.commit()
    conn.close()


def _schema_names(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    finally:
        conn.close()


def test_triggers_and_indexes_exist_right_after_the_migration(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite"
    _old_journal(path)
    Journal(path)  # nothing else has run: the migration's own commit must leave them in place
    assert {
        "attempt_journal_no_update",
        "attempt_journal_no_delete",
        "attempt_journal_task",
        "attempt_journal_one_dispatch",
    } <= _schema_names(path)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        sqlite3.connect(path).execute("UPDATE attempt_journal SET detail = 'x'")


def test_openers_racing_on_an_old_journal_all_end_with_the_new_one(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite"
    _old_journal(path)
    errors: list[BaseException] = []

    def open_journal() -> None:
        try:
            Journal(path)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=open_journal) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert [e.phase for e in Journal(path).entries("t")] == ["DISPATCHED"]
    assert "attempt_journal_no_delete" in _schema_names(path)
    Journal(path).record("t", "a", "TEST_SOURCE", "auto")


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
