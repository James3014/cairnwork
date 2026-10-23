"""The notary's on-disk schema contract: the version and the DDL the Notary creates and migrates.

Kept apart from the runtime logic in notary.py so the append-only record's shape and its migration
history are read in one place. The Notary is the only writer of this schema.
"""

from __future__ import annotations

SCHEMA_VERSION = 3

# expected_identity (schema 2, Issue #74): the Core identity a row was notarized against, frozen so
# readback survives a later Core upgrade instead of reading back as CORE_IDENTITY_MISMATCH. Nullable
# so the v1->v2 migration can ALTER it onto an append-only table that forbids backfilling;
# pre-migration rows stay NULL and keep the read-time behaviour. New rows always set it.
# binding_json (schema 3, Issue #229): an external candidate's binding (rule, base, head, test
# commit and paths, executed tree, verifier argv); NULL for every other row (a commit target).
SCHEMA = """
CREATE TABLE IF NOT EXISTS core_verifications (
    seq               INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id           TEXT NOT NULL,
    attempt_id        TEXT NOT NULL,
    base_commit       TEXT NOT NULL,
    candidate_commit  TEXT NOT NULL,
    request_hash      TEXT NOT NULL,
    request_json      TEXT NOT NULL,
    http_status       INTEGER NOT NULL,
    status            TEXT NOT NULL,
    integrity         TEXT NOT NULL,
    response_json     TEXT NOT NULL,
    recorded_at       TEXT NOT NULL,
    expected_identity TEXT,
    binding_json      TEXT,
    UNIQUE (task_id, attempt_id)
);
CREATE TRIGGER IF NOT EXISTS core_verifications_no_update
    BEFORE UPDATE ON core_verifications
    BEGIN SELECT RAISE(ABORT, 'core_verifications is append-only'); END;
CREATE TRIGGER IF NOT EXISTS core_verifications_no_delete
    BEFORE DELETE ON core_verifications
    BEGIN SELECT RAISE(ABORT, 'core_verifications is append-only'); END;
CREATE TRIGGER IF NOT EXISTS core_verifications_no_overwrite
    BEFORE INSERT ON core_verifications
    WHEN EXISTS (SELECT 1 FROM core_verifications
                 WHERE task_id = NEW.task_id AND attempt_id = NEW.attempt_id)
    BEGIN SELECT RAISE(ABORT, 'core_verifications is append-only'); END;
"""

# One ALTER per older version, applied in order by Notary._migrate (#74, #229).
MIGRATIONS = {
    1: "ALTER TABLE core_verifications ADD COLUMN expected_identity TEXT",
    2: "ALTER TABLE core_verifications ADD COLUMN binding_json TEXT",
}
