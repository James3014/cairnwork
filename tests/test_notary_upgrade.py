"""Issue #74: a historic verdict must survive a Core upgrade, and a v1 ledger must migrate."""

import sqlite3

from test_notary import notarize

import cairnwork.notary as notary_module
from cairnwork.notary import SCHEMA_VERSION, Notary

_OTHER_IDENTITY = {"schema": "nexus.core.core-identity.v1-experimental", "version": "9.9.9"}

_V1_SCHEMA = (
    "CREATE TABLE core_verifications ("
    " seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, attempt_id TEXT NOT NULL,"
    " base_commit TEXT NOT NULL, candidate_commit TEXT NOT NULL, request_hash TEXT NOT NULL,"
    " request_json TEXT NOT NULL, http_status INTEGER NOT NULL, status TEXT NOT NULL,"
    " integrity TEXT NOT NULL, response_json TEXT NOT NULL, recorded_at TEXT NOT NULL,"
    " UNIQUE (task_id, attempt_id));"
)
_V1_COLS = (
    "task_id, attempt_id, base_commit, candidate_commit, request_hash, request_json,"
    " http_status, status, integrity, response_json, recorded_at"
)


def _build_v1_ledger(src_path, dst_path):
    """A schema-1 ledger (no expected_identity column) holding one real row copied from src."""
    src = sqlite3.connect(src_path)
    values = src.execute(f"SELECT {_V1_COLS} FROM core_verifications").fetchone()
    src.close()
    conn = sqlite3.connect(dst_path)
    conn.executescript(_V1_SCHEMA)
    conn.execute("PRAGMA user_version = 1")
    conn.execute(
        f"INSERT INTO core_verifications ({_V1_COLS}) VALUES ({','.join('?' * 11)})", values
    )
    conn.commit()
    conn.close()


def test_a_verified_row_survives_a_core_identity_change(repo, tmp_path, monkeypatch):
    notary = Notary(tmp_path / "n.sqlite")
    verdict = notarize(notary, repo, {"src/a.py": "x = 2\n"})
    assert verdict.verified
    # Simulate a Core upgrade: an unpinned notary would now expect a different identity than the one
    # the row was notarized against. The stored expectation must still verify the historic row.
    monkeypatch.setattr(notary_module, "default_core_identity", lambda: _OTHER_IDENTITY)
    reopened = Notary(tmp_path / "n.sqlite")
    again = reopened.verdict("task-1", "a1", expected_candidate=verdict.candidate_commit)
    assert again is not None and again.verified and again.reason == ""


def test_an_unpinned_row_still_requires_a_later_pin_to_match(repo, tmp_path):
    notary = Notary(tmp_path / "n.sqlite")  # unpinned: stores the local Core identity
    verdict = notarize(notary, repo, {"src/a.py": "x = 2\n"})
    assert verdict.verified
    pinned = Notary(tmp_path / "n.sqlite", core_identity=_OTHER_IDENTITY)
    got = pinned.verdict("task-1", "a1", expected_candidate=verdict.candidate_commit)
    assert got is not None and not got.verified and got.reason == "CORE_IDENTITY_MISMATCH"


def test_a_v1_ledger_migrates_and_its_rows_keep_the_read_time_identity(repo, tmp_path, monkeypatch):
    v = notarize(Notary(tmp_path / "src.sqlite"), repo, {"src/a.py": "x = 2\n"})
    path = tmp_path / "v1.sqlite"
    _build_v1_ledger(tmp_path / "src.sqlite", path)
    migrated = Notary(path)  # migrates v1 -> v2; the old row's expected_identity stays NULL
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    columns = {r[1] for r in conn.execute("PRAGMA table_info(core_verifications)")}
    conn.close()
    assert "expected_identity" in columns
    got = migrated.verdict("task-1", "a1", expected_candidate=v.candidate_commit)
    assert got is not None and got.verified  # NULL expectation -> read-time identity still matches
    # A pre-migration row keeps the read-time behaviour: after an upgrade it reads unverified (the
    # documented interim — settle running tasks before upgrading).
    monkeypatch.setattr(notary_module, "default_core_identity", lambda: _OTHER_IDENTITY)
    after = Notary(path).verdict("task-1", "a1", expected_candidate=v.candidate_commit)
    assert after is not None and not after.verified
