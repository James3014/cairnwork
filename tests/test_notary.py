import sqlite3

import pytest
from conftest import commit_change, git
from product.adapters.generic_verification import verify_generic_changeset

from cairnwork.changeset import read_change_set
from cairnwork.core_request import AcceptanceSpec, VerifierObservation, build_core_request
from cairnwork.notary import CORE_ERROR, Notary

PASS = (VerifierObservation("pytest", "artifact-1", "sha256:" + "e" * 64, True),)
FAIL = (VerifierObservation("pytest", "artifact-1", "sha256:" + "e" * 64, False),)


def _spec(allowed=("src/a.py",), verifiers=("pytest",), deletions=False):
    return AcceptanceSpec("task-1", "sha256:" + "c" * 64, verifiers, allowed, deletions)


def _request(repo, files, *, spec=None, observations=PASS, delete=(), attempt="a1"):
    base = git(repo, "rev-parse", "HEAD")
    head = commit_change(repo, files, delete=delete)
    change = read_change_set(repo, base, head)
    return build_core_request(spec or _spec(), change, observations, attempt_id=attempt)


@pytest.fixture
def notary(tmp_path):
    return Notary(tmp_path / "notary.sqlite")


def test_in_scope_change_with_passing_verifier_is_verified(repo, notary):
    verdict = notary.notarize("task-1", "a1", _request(repo, {"src/a.py": "x = 2\n"}))
    assert verdict.http_status == 200
    assert verdict.status == "VERIFIED" and verdict.integrity == "VALID"
    assert verdict.verified


@pytest.mark.parametrize(
    "kwargs",
    [
        {"files": {"src/b.py": "y = 2\n"}},
        {"files": {"src/a.py": "x = 2\n"}, "observations": FAIL},
        {"files": {"src/a.py": "x = 2\n"}, "spec": _spec(verifiers=("pytest", "lint"))},
        {"files": {}, "delete": ["src/a.py"]},
    ],
    ids=["out-of-scope", "failing-verifier", "missing-verifier", "deletion-forbidden"],
)
def test_core_rejections_are_never_verified(repo, notary, kwargs):
    verdict = notary.notarize("task-1", "a1", _request(repo, **kwargs))
    assert not verdict.verified
    assert verdict.status != "VERIFIED"


def test_first_outcome_of_an_attempt_is_final(repo, notary):
    failed = notary.notarize(
        "task-1", "a1", _request(repo, {"src/a.py": "x = 2\n"}, observations=FAIL)
    )
    later = notary.notarize("task-1", "a1", _request(repo, {"src/a.py": "x = 3\n"}))
    assert not failed.verified
    assert later == failed
    fresh = notary.notarize("task-1", "a2", _request(repo, {"src/a.py": "x = 4\n"}, attempt="a2"))
    assert fresh.verified


def test_record_is_append_only(repo, notary, tmp_path):
    notary.notarize("task-1", "a1", _request(repo, {"src/a.py": "x = 2\n"}, observations=FAIL))
    conn = sqlite3.connect(tmp_path / "notary.sqlite")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE core_verifications SET status = 'VERIFIED'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM core_verifications")
    conn.execute("PRAGMA recursive_triggers=ON")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT OR REPLACE INTO core_verifications (task_id, attempt_id, request_hash,"
            " http_status, status, integrity, response_json, recorded_at)"
            " VALUES ('task-1', 'a1', 'h', 200, 'VERIFIED', 'VALID', '{}', 'now')"
        )
    conn.close()
    assert not Notary(tmp_path / "notary.sqlite").verdict("task-1", "a1").verified


def test_core_exception_is_recorded_and_not_verified(repo, tmp_path):
    def broken(_request):
        raise RuntimeError("core down")

    notary = Notary(tmp_path / "n.sqlite", core_verify=broken)
    verdict = notary.notarize("task-1", "a1", _request(repo, {"src/a.py": "x = 2\n"}))
    assert not verdict.verified
    assert verdict.status == CORE_ERROR and verdict.http_status == 0


@pytest.mark.parametrize(
    "reply", [None, (200, "VERIFIED"), ("200", {}), (422, {"error": {"code": "X"}})]
)
def test_malformed_or_error_responses_are_not_verified(repo, tmp_path, reply):
    notary = Notary(tmp_path / "n.sqlite", core_verify=lambda _r: reply)
    assert not notary.notarize("task-1", "a1", _request(repo, {"src/a.py": "x = 2\n"})).verified


def test_forged_verified_response_with_other_hashes_is_not_verified(repo, tmp_path):
    forged = {
        "verification": {"status": "VERIFIED", "integrity": "VALID", "reason_codes": []},
        "hashes": {
            k: "sha256:" + "0" * 64
            for k in (
                "acceptance_contract_hash",
                "change_set_hash",
                "verification_plan_hash",
                "evidence_bundle_hash",
            )
        },
    }
    notary = Notary(tmp_path / "n.sqlite", core_verify=lambda _r: (200, forged))
    assert not notary.notarize("task-1", "a1", _request(repo, {"src/a.py": "x = 2\n"})).verified


def test_verdict_survives_reopening(repo, tmp_path):
    path = tmp_path / "n.sqlite"
    first = Notary(path).notarize("task-1", "a1", _request(repo, {"src/a.py": "x = 2\n"}))
    assert Notary(path).verdict("task-1", "a1") == first


@pytest.mark.parametrize("change", ["http", "integrity"])
def test_verified_status_alone_is_not_enough(repo, tmp_path, change):
    def tampered(request):
        status, response = verify_generic_changeset(request)
        assert response["verification"]["status"] == "VERIFIED"
        if change == "http":
            return 201, response
        response["verification"]["integrity"] = "INVALID"
        return status, response

    notary = Notary(tmp_path / "n.sqlite", core_verify=tampered)
    verdict = notary.notarize("task-1", "a1", _request(repo, {"src/a.py": "x = 2\n"}))
    assert verdict.status == "VERIFIED"
    assert not verdict.verified
