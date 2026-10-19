import sqlite3
import threading
import time

import pytest
from conftest import commit_change, git
from product.adapters.generic_verification import verify_generic_changeset

from cairnwork.changeset import read_change_set
from cairnwork.core_request import AcceptanceSpec, VerifierObservation
from cairnwork.notary import CORE_ERROR, AttemptRequestMismatch, Notary, NotaryInputError

PASS = (VerifierObservation("pytest", "artifact-1", "sha256:" + "e" * 64, True),)
FAIL = (VerifierObservation("pytest", "artifact-1", "sha256:" + "e" * 64, False),)


def spec(allowed=("src/a.py",), verifiers=("pytest",), deletions=False, task="task-1"):
    return AcceptanceSpec(task, "sha256:" + "c" * 64, verifiers, allowed, deletions)


def change(repo, files=None, delete=()):
    base = git(repo, "rev-parse", "HEAD")
    head = commit_change(repo, files or {}, delete=delete)
    return read_change_set(repo, base, head)


def notarize(notary, repo, files=None, *, s=None, obs=PASS, delete=(), attempt="a1"):
    return notary.notarize("task-1", attempt, s or spec(), change(repo, files, delete), obs)


@pytest.fixture
def notary(tmp_path):
    return Notary(tmp_path / "notary.sqlite")


def test_in_scope_change_with_passing_verifier_is_verified(repo, notary):
    verdict = notarize(notary, repo, {"src/a.py": "x = 2\n"})
    assert verdict.http_status == 200
    assert verdict.status == "VERIFIED" and verdict.integrity == "VALID"
    assert verdict.verified
    assert verdict.candidate_commit == git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"files": {"src/b.py": "y = 2\n"}},
        {"files": {"src/a.py": "x = 2\n"}, "obs": FAIL},
        {"files": {"src/a.py": "x = 2\n"}, "s": spec(verifiers=("pytest", "lint"))},
        {"delete": ["src/a.py"]},
    ],
    ids=["out-of-scope", "failing-verifier", "missing-verifier", "deletion-forbidden"],
)
def test_core_rejections_are_never_verified(repo, notary, kwargs):
    verdict = notarize(notary, repo, **kwargs)
    assert not verdict.verified
    assert verdict.status != "VERIFIED"


def test_verified_attempt_cannot_be_replayed_for_another_change(repo, notary):
    assert notarize(notary, repo, {"src/a.py": "x = 2\n"}).verified
    with pytest.raises(AttemptRequestMismatch):
        notarize(notary, repo, {"src/b.py": "y = 9\n"}, obs=FAIL)
    with pytest.raises(AttemptRequestMismatch):
        notarize(notary, repo, {"src/a.py": "x = 3\n"})


def test_failed_attempt_stays_failed_and_core_is_called_once(repo, tmp_path):
    calls = []

    def counting(request):
        calls.append(request)
        return verify_generic_changeset(request)

    notary = Notary(tmp_path / "notary.sqlite", core_verify=counting)
    the_change = change(repo, {"src/a.py": "x = 2\n"})
    failed = notary.notarize("task-1", "a1", spec(), the_change, FAIL)
    again = notary.notarize("task-1", "a1", spec(), the_change, FAIL)
    assert not failed.verified and again == failed
    assert len(calls) == 1
    fresh = notary.notarize("task-1", "a2", spec(), change(repo, {"src/a.py": "x = 4\n"}), PASS)
    assert fresh.verified


def test_spec_must_belong_to_the_task(repo, notary):
    with pytest.raises(NotaryInputError):
        notary.notarize("task-1", "a1", spec(task="OTHER"), change(repo, {"src/a.py": "x\n"}), PASS)


def test_reader_must_hold_the_verified_commit(repo, notary):
    verdict = notarize(notary, repo, {"src/a.py": "x = 2\n"})
    same = notary.verdict("task-1", "a1", expected_candidate=verdict.candidate_commit)
    other = notary.verdict("task-1", "a1", expected_candidate=verdict.base_commit)
    assert same.verified
    assert not other.verified


def test_record_is_append_only_for_any_connection(repo, notary, tmp_path):
    notarize(notary, repo, {"src/a.py": "x = 2\n"}, obs=FAIL)
    conn = sqlite3.connect(tmp_path / "notary.sqlite")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE core_verifications SET status = 'VERIFIED'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM core_verifications")
    with pytest.raises(sqlite3.IntegrityError):  # recursive_triggers left OFF on purpose
        conn.execute(
            "INSERT OR REPLACE INTO core_verifications (task_id, attempt_id, base_commit,"
            " candidate_commit, request_hash, request_json, http_status, status, integrity,"
            " response_json, recorded_at) VALUES ('task-1', 'a1', 'b', 'c', 'h', '{}', 200,"
            " 'VERIFIED', 'VALID', '{}', 'now')"
        )
    conn.close()
    assert not notary.verdict("task-1", "a1").verified


def test_tampered_stored_request_is_not_verified(repo, notary, tmp_path):
    notarize(notary, repo, {"src/a.py": "x = 2\n"})
    conn = sqlite3.connect(tmp_path / "notary.sqlite")
    conn.execute("DROP TRIGGER core_verifications_no_update")
    conn.execute("UPDATE core_verifications SET request_json = '{}'")
    conn.commit()
    conn.close()
    assert not notary.verdict("task-1", "a1").verified


def test_core_exception_is_recorded_and_not_verified(repo, tmp_path):
    def broken(_request):
        raise RuntimeError("core down")

    verdict = notarize(Notary(tmp_path / "n.sqlite", core_verify=broken), repo, {"src/a.py": "x\n"})
    assert not verdict.verified
    assert verdict.status == CORE_ERROR and verdict.http_status == 0


@pytest.mark.parametrize(
    "reply", [None, (200, "VERIFIED"), ("200", {}), (True, {}), (422, {"error": {"code": "X"}})]
)
def test_malformed_or_error_responses_are_not_verified(repo, tmp_path, reply):
    notary = Notary(tmp_path / "n.sqlite", core_verify=lambda _r: reply)
    assert not notarize(notary, repo, {"src/a.py": "x = 2\n"}).verified


def test_forged_verified_response_with_other_hashes_is_not_verified(repo, tmp_path):
    keys = ("acceptance_contract_hash", "change_set_hash", "verification_plan_hash")
    forged = {
        "verification": {"status": "VERIFIED", "integrity": "VALID", "reason_codes": []},
        "hashes": {k: "sha256:" + "0" * 64 for k in (*keys, "evidence_bundle_hash")},
    }
    notary = Notary(tmp_path / "n.sqlite", core_verify=lambda _r: (200, forged))
    assert not notarize(notary, repo, {"src/a.py": "x = 2\n"}).verified


@pytest.mark.parametrize("tamper", ["http", "integrity"])
def test_verified_status_alone_is_not_enough(repo, tmp_path, tamper):
    def tampered(request):
        status, response = verify_generic_changeset(request)
        assert response["verification"]["status"] == "VERIFIED"
        if tamper == "http":
            return 201, response
        response["verification"]["integrity"] = "INVALID"
        return status, response

    verdict = notarize(
        Notary(tmp_path / "n.sqlite", core_verify=tampered), repo, {"src/a.py": "x\n"}
    )
    assert verdict.status == "VERIFIED"
    assert not verdict.verified


def test_concurrent_notarize_of_one_attempt_returns_one_recorded_verdict(repo, tmp_path):
    def slow(request):
        time.sleep(0.3)
        return verify_generic_changeset(request)

    notary = Notary(tmp_path / "n.sqlite", core_verify=slow)
    the_change = change(repo, {"src/a.py": "x = 2\n"})
    results, errors = [], []

    def run():
        try:
            results.append(notary.notarize("task-1", "a1", spec(), the_change, PASS))
        except Exception as exc:  # noqa: BLE001 - collected for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len({r.request_hash for r in results}) == 1 and len(results) == 4
    assert all(r.verified for r in results)


def test_verdict_survives_reopening(repo, tmp_path):
    path = tmp_path / "n.sqlite"
    first = notarize(Notary(path), repo, {"src/a.py": "x = 2\n"})
    assert Notary(path).verdict("task-1", "a1") == first


def test_tampered_stored_request_hash_is_not_verified(repo, notary, tmp_path):
    notarize(notary, repo, {"src/a.py": "x = 2\n"})
    conn = sqlite3.connect(tmp_path / "notary.sqlite")
    conn.execute("DROP TRIGGER core_verifications_no_update")
    conn.execute("UPDATE core_verifications SET request_hash = 'sha256:forged'")
    conn.commit()
    conn.close()
    assert not notary.verdict("task-1", "a1").verified
