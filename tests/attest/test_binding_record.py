"""#229 in the notary: a bound row (schema 3) names the PR head and the executed tree, a binding
that does not match is never sent to Core, an edited binding reads back unverified, and older
ledgers migrate with ALTER."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from cairnwork.notary import Notary, NotaryInputError
from cairnwork_attest.record import AttestError, read_record, verify_record
from cairnwork_attest.verify import verify_bundle
from tests.attest.binding_helpers import (
    TESTS,
    commit_on,
    git,
    notarize_bound,
    policy,
    producer,
    receiver,
    sign_bound,
)
from tests.attest.test_verify import CORE_IDENTITY, fake_core_verify


def _row(state: Path) -> tuple:
    conn = sqlite3.connect(state / "notary.sqlite")
    try:
        return conn.execute(
            "SELECT candidate_commit, request_json, binding_json FROM core_verifications"
        ).fetchone()
    finally:
        conn.close()


def test_a_bound_row_names_the_head_and_the_tree_that_ran(tmp_path):
    p = producer(tmp_path)
    run = notarize_bound(tmp_path / "s", p)
    assert run["verdict"].verified and run["verdict"].candidate_commit == p["head"]
    candidate, request, binding = _row(tmp_path / "s")
    change_set = json.loads(request)["change_set"]
    assert candidate == p["head"] and json.loads(binding) == run["binding"]
    assert change_set["target_revision"] == f"git-tree:{run['binding']['executed_tree']}"
    assert change_set["source_revision"] == f"git-commit:{p['base']}"
    notary = Notary(tmp_path / "s/notary.sqlite", core_verify=fake_core_verify)
    bound = notary.verdict(run["task"], "a1", expected_candidate=p["head"])
    scratch = notary.verdict(run["task"], "a1", expected_candidate=run["judged"])
    assert bound is not None and bound.verified
    assert scratch is not None and not scratch.verified


@pytest.mark.parametrize("field", ["executed_tree", "base", "requirements_hash", "head"])
def test_a_binding_that_does_not_match_never_reaches_core(tmp_path, field):
    from cairnwork.core_request import AcceptanceSpec
    from cairnwork.notary_verify import binding_hash
    from tests.attest.binding_helpers import executed

    p = producer(tmp_path)
    tree, judged = executed(p["work"], p["head"], p["tests"], ("tests/test_new.py",))
    binding = {"rule": "overlay-v1", "base": p["base"], "head": p["head"],
               "test_commit": p["tests"], "test_paths": ["tests/test_new.py"],
               "executed_tree": tree, "verifiers": []}  # fmt: skip
    if field != "requirements_hash":
        binding[field] = "nothead" if field == "head" else "1" * 40
    digest = binding_hash(binding)  # consistent with the edited binding: only `field` is wrong
    if field == "requirements_hash":
        digest = "sha256:" + "0" * 64
    calls = []
    notary = Notary(tmp_path / "n.sqlite", core_verify=lambda r: calls.append(r) or (500, {}))
    spec = AcceptanceSpec("verify-x", digest, ("v",), ("src/a.py",))
    with pytest.raises(NotaryInputError):
        notary.notarize("verify-x", "a1", spec, p["work"], p["base"], judged, (), binding)
    assert calls == []


def test_an_edited_binding_reads_back_unverified_and_is_not_signed(tmp_path):
    p = producer(tmp_path)
    state = tmp_path / "s"
    run = notarize_bound(state, p)
    conn = sqlite3.connect(state / "notary.sqlite")
    conn.execute("DROP TRIGGER core_verifications_no_update")
    edited = {**run["binding"], "test_paths": ["tests/test_new.py"]}
    conn.execute("UPDATE core_verifications SET binding_json = ?", (json.dumps(edited),))
    conn.commit()
    conn.close()
    notary = Notary(state / "notary.sqlite", core_verify=fake_core_verify)
    got = notary.verdict(run["task"], "a1", expected_candidate=p["head"])
    assert got is not None and not got.verified
    with pytest.raises(AttestError) as exc:
        verify_record(state / "notary.sqlite", run["task"], "a1", expected_candidate=p["head"],
                      core_identity=CORE_IDENTITY)  # fmt: skip
    assert exc.value.code == "BINDING_HASH_MISMATCH"


def _raw_overlay(work: Path, head: str, tests: str, paths: tuple[str, ...], index: Path) -> str:
    """What a buggy overlay would build: test entries copied whatever their mode."""
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}

    def run(*args: str) -> str:
        done = subprocess.run(["git", "-C", str(work), *args], env=env, check=True,
                              capture_output=True, text=True)  # fmt: skip
        return done.stdout.strip()

    run("read-tree", head)
    for path in paths:
        mode, _, blob = git(work, "ls-tree", tests, "--", path).split("\t")[0].split()
        run("update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}")
    return run("write-tree")


def test_a_symlink_laid_over_the_head_is_refused_by_the_receiver(tmp_path):
    p = producer(tmp_path)
    work = p["work"]
    git(work, "checkout", "-q", "--detach", p["base"])
    (work / "tests/link").symlink_to("test_a.py")
    tests = commit_on(work, p["base"], TESTS, "tests with a link")  # tests/link is 120000
    paths = ("tests/link", *sorted(TESTS))
    tree = _raw_overlay(work, p["head"], tests, paths, tmp_path / "i")
    judged = git(work, "commit-tree", tree, "-p", p["head"], "-m", "scratch")
    run = notarize_bound(tmp_path / "s", {**p, "tests": tests}, paths=paths,
                         tree_and_judged=(tree, judged))  # fmt: skip
    bundle = sign_bound(tmp_path / "s", run, tmp_path / "b.json")
    full = receiver(tmp_path, "full", work, p["head"], tests)
    conf = receiver(tmp_path, "conf", work, p["head"])
    kw = {"public_key_pem": tmp_path / "s/public.pem", "expected_candidate": p["head"]}
    assert verify_bundle(bundle, repo=full, **kw).code == "OVERLAY_UNSUPPORTED_ENTRY"
    pol = policy(tmp_path / "s", tmp_path, tests)
    assert verify_bundle(bundle, repo=conf, policy=pol, **kw).code == "OVERLAY_UNSUPPORTED_ENTRY"


_V2_COLUMNS = (
    "task_id, attempt_id, base_commit, candidate_commit, request_hash, request_json,"
    " http_status, status, integrity, response_json, recorded_at, expected_identity"
)


def test_a_v2_ledger_is_read_as_is_and_migrates_to_v3(tmp_path, repo):
    from tests.attest.test_verify import setup_verified_bundle_fixture

    src = tmp_path / "src"
    _bundle, _pub, _base, cand = setup_verified_bundle_fixture(src, repo)
    path = tmp_path / "v2.sqlite"
    conn = sqlite3.connect(src / "notary.sqlite")
    values = conn.execute(f"SELECT {_V2_COLUMNS} FROM core_verifications").fetchone()
    conn.close()
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE core_verifications (seq INTEGER PRIMARY KEY AUTOINCREMENT,"
        " task_id TEXT NOT NULL, attempt_id TEXT NOT NULL, base_commit TEXT NOT NULL,"
        " candidate_commit TEXT NOT NULL, request_hash TEXT NOT NULL, request_json TEXT NOT NULL,"
        " http_status INTEGER NOT NULL, status TEXT NOT NULL, integrity TEXT NOT NULL,"
        " response_json TEXT NOT NULL, recorded_at TEXT NOT NULL, expected_identity TEXT,"
        " UNIQUE (task_id, attempt_id)); PRAGMA user_version = 2;"
    )
    conn.execute(f"INSERT INTO core_verifications ({_V2_COLUMNS}) VALUES ({','.join('?' * 12)})",
                 values)  # fmt: skip
    conn.commit()
    conn.close()
    assert read_record(path, "task-1", "a1").binding is None  # read-only, before any migration
    verified = verify_record(path, "task-1", "a1", expected_candidate=cand,
                             core_identity=CORE_IDENTITY)  # fmt: skip
    assert verified.verdict.verified and verified.record.binding is None
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert "binding_json" in {r[1] for r in conn.execute("PRAGMA table_info(core_verifications)")}
    assert conn.execute("SELECT binding_json FROM core_verifications").fetchone() == (None,)
    conn.close()
