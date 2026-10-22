"""Offline verification of Cairnwork DSSE attestation bundles."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cairnwork.changeset import ChangeSetError, read_change_set
from cairnwork.gitcmd import GitCommandError, run_git
from cairnwork_attest.bundle import BundleError, read_bundle
from cairnwork_attest.dsse import verify_envelope
from cairnwork_attest.keys import KeyFileError, load_public_key_pem
from cairnwork_attest.record import _digest, parse_strict_json
from cairnwork_attest.statement import PREDICATE_TYPE, STATEMENT_TYPE
from cairnwork_protocol import (
    acceptance_contract_hash,
    change_manifest_hash,
    change_set_hash,
    evidence_bundle_hash,
    response_binding_problem,
    verification_plan_hash,
)

_OID = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    code: str
    detail: str = ""
    statement: dict[str, Any] | None = None

    def __bool__(self) -> bool:
        return self.ok


def _fail(code: str, detail: str = "") -> VerifyResult:
    return VerifyResult(ok=False, code=code, detail=detail)


def _verify_envelope_and_payload(
    bundle_path: Path, public_key_pem: Path
) -> tuple[bytes | None, VerifyResult | None]:
    try:
        pem_bytes = Path(public_key_pem).read_bytes()
        pubkey = load_public_key_pem(pem_bytes)
    except (KeyFileError, OSError) as exc:
        return None, _fail("SIGNATURE_INVALID", f"cannot load public key: {exc}")

    try:
        envelope = read_bundle(Path(bundle_path))
    except (BundleError, OSError) as exc:
        return None, _fail("SIGNATURE_INVALID", f"cannot read bundle: {exc}")

    verif = verify_envelope(envelope, pubkey)
    if not verif.ok or verif.payload is None:
        return None, _fail("SIGNATURE_INVALID", f"signature invalid: {verif.reason}")

    return verif.payload, None


def _parse_statement_payload(
    payload_bytes: bytes,
) -> tuple[dict[str, Any] | None, VerifyResult | None]:
    try:
        text = payload_bytes.decode("utf-8")
        parsed = parse_strict_json(
            text,
            error_cls=BundleError,
            malformed_code="STATEMENT_SHAPE",
            constant_code="STATEMENT_SHAPE",
        )
    except (BundleError, UnicodeDecodeError) as exc:
        return None, _fail("STATEMENT_SHAPE", f"statement JSON malformed: {exc}")

    if not isinstance(parsed, dict):
        return None, _fail("STATEMENT_SHAPE", "statement is not a JSON object")

    if parsed.get("_type") != STATEMENT_TYPE:
        return None, _fail("STATEMENT_SHAPE", f"_type is not {STATEMENT_TYPE}")
    if parsed.get("predicateType") != PREDICATE_TYPE:
        return None, _fail("STATEMENT_SHAPE", f"predicateType is not {PREDICATE_TYPE}")
    if not isinstance(parsed.get("predicate"), dict):
        return None, _fail("STATEMENT_SHAPE", "predicate is not an object")

    return parsed, None


def _check_subject_and_commits(
    statement: dict[str, Any], repo: Path
) -> tuple[str | None, str | None, VerifyResult | None]:
    subjects = statement.get("subject")
    if not isinstance(subjects, list) or len(subjects) != 1:
        return None, None, _fail("SUBJECT_SHAPE", "subject must be a list with exactly one item")

    subj = subjects[0]
    if not isinstance(subj, dict):
        return None, None, _fail("SUBJECT_SHAPE", "subject entry is not an object")

    digest = subj.get("digest")
    if not isinstance(digest, dict):
        return None, None, _fail("SUBJECT_SHAPE", "subject digest is not an object")

    candidate = digest.get("gitCommit")
    if not isinstance(candidate, str) or not _OID.fullmatch(candidate):
        return None, None, _fail("SUBJECT_SHAPE", f"invalid candidate commit {candidate!r}")

    predicate = statement["predicate"]
    base = predicate.get("base_commit")
    if not isinstance(base, str) or not _OID.fullmatch(base):
        return None, None, _fail("SUBJECT_SHAPE", f"invalid base commit {base!r}")

    repo_path = Path(repo)
    for sha in (base, candidate):
        try:
            run_git(repo_path, "cat-file", "-e", f"{sha}^{{commit}}")
        except GitCommandError as exc:
            return None, None, _fail("COMMIT_NOT_FOUND", f"commit {sha} not found in repo: {exc}")

    return candidate, base, None


def _check_request_binding(
    predicate: dict[str, Any], candidate: str, base: str
) -> VerifyResult | None:
    request = predicate.get("request")
    if not isinstance(request, dict):
        return _fail("REQUEST_HASH_MISMATCH", "predicate.request is not an object")

    expected_req_hash = _digest(request)
    if predicate.get("request_hash") != expected_req_hash:
        return _fail(
            "REQUEST_HASH_MISMATCH",
            f"expected {expected_req_hash}, got {predicate.get('request_hash')!r}",
        )

    change_set = request.get("change_set")
    if not isinstance(change_set, dict):
        return _fail("COMMIT_BINDING_MISMATCH", "request missing change_set object")

    target_rev = change_set.get("target_revision")
    source_rev = change_set.get("source_revision")
    if target_rev != f"git-commit:{candidate}":
        return _fail(
            "COMMIT_BINDING_MISMATCH",
            f"target_revision {target_rev!r} != git-commit:{candidate}",
        )
    if source_rev != f"git-commit:{base}":
        return _fail(
            "COMMIT_BINDING_MISMATCH",
            f"source_revision {source_rev!r} != git-commit:{base}",
        )

    return None


def _check_manifest(
    repo: Path, predicate: dict[str, Any], candidate: str, base: str
) -> VerifyResult | None:
    try:
        change = read_change_set(Path(repo), base, candidate)
        manifest = {
            "source_tree": f"git-tree:{change.base_tree}",
            "target_tree": f"git-tree:{change.candidate_tree}",
            "entries": [entry.to_core() for entry in change.entries],
        }
        recomputed_hash = change_manifest_hash(manifest)
    except (ChangeSetError, GitCommandError, TypeError, ValueError) as exc:
        return _fail("MANIFEST_MISMATCH", f"cannot build change manifest: {exc}")

    request = predicate["request"]
    change_set = request["change_set"]
    if change_set.get("diff_hash") != recomputed_hash:
        return _fail(
            "MANIFEST_MISMATCH",
            f"change_set.diff_hash {change_set.get('diff_hash')!r} != {recomputed_hash}",
        )

    response = predicate.get("response")
    if isinstance(response, dict):
        hashes = response.get("hashes")
        if isinstance(hashes, dict) and "change_manifest_hash" in hashes:
            resp_h = hashes["change_manifest_hash"]
            if resp_h != recomputed_hash:
                return _fail(
                    "MANIFEST_MISMATCH",
                    f"response change_manifest_hash {resp_h!r} != {recomputed_hash}",
                )

    return None


def _check_response_verified(predicate: dict[str, Any]) -> VerifyResult | None:
    response = predicate.get("response")
    if not isinstance(response, dict):
        return _fail("RESPONSE_NOT_VERIFIED", "response is not an object")

    verif = response.get("verification")
    if not isinstance(verif, dict):
        return _fail("RESPONSE_NOT_VERIFIED", "response missing verification object")

    if verif.get("status") != "VERIFIED" or verif.get("integrity") != "VALID":
        return _fail(
            "RESPONSE_NOT_VERIFIED",
            f"status={verif.get('status')!r}, integrity={verif.get('integrity')!r}",
        )

    if predicate.get("status") != "VERIFIED" or predicate.get("integrity") != "VALID":
        st = predicate.get("status")
        integ = predicate.get("integrity")
        return _fail("RESPONSE_NOT_VERIFIED", f"predicate status={st!r}, integrity={integ!r}")

    if predicate.get("http_status") != 200:
        return _fail("RESPONSE_NOT_VERIFIED", f"http_status={predicate.get('http_status')!r}")

    core_id = response.get("core_identity")
    if not isinstance(core_id, dict):
        return _fail("RESPONSE_NOT_VERIFIED", "response missing core_identity")

    request = predicate["request"]
    binding_problem = response_binding_problem(request, response, core_id)
    if binding_problem is not None:
        return _fail("RESPONSE_NOT_VERIFIED", f"response binding problem: {binding_problem}")

    hashes = response.get("hashes")
    if not isinstance(hashes, dict):
        return _fail("RESPONSE_NOT_VERIFIED", "response missing hashes object")

    try:
        expected = {
            "acceptance_contract_hash": acceptance_contract_hash(request["acceptance_contract"]),
            "change_set_hash": change_set_hash(request["change_set"]),
            "verification_plan_hash": verification_plan_hash(request["verification_plan"]),
            "evidence_bundle_hash": evidence_bundle_hash(request["evidence_bundle"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        return _fail("RESPONSE_NOT_VERIFIED", f"malformed request for hashes: {exc}")

    for key, exp_val in expected.items():
        if hashes.get(key) != exp_val:
            return _fail("RESPONSE_NOT_VERIFIED", f"hash mismatch for {key}: expected {exp_val}")

    return None


def verify_bundle(
    bundle_path: Path,
    *,
    public_key_pem: Path,
    repo: Path,
) -> VerifyResult:
    """Verify an attestation bundle against local git objects and a trusted public key."""
    payload_bytes, err = _verify_envelope_and_payload(bundle_path, public_key_pem)
    if err is not None:
        return err
    if payload_bytes is None:
        return _fail("SIGNATURE_INVALID")

    statement, err = _parse_statement_payload(payload_bytes)
    if err is not None:
        return err
    if statement is None:
        return _fail("STATEMENT_SHAPE")

    candidate, base, err = _check_subject_and_commits(statement, repo)
    if err is not None:
        return err
    if candidate is None or base is None:
        return _fail("SUBJECT_SHAPE")

    predicate = statement["predicate"]

    err = _check_request_binding(predicate, candidate, base)
    if err is not None:
        return err

    err = _check_manifest(repo, predicate, candidate, base)
    if err is not None:
        return err

    err = _check_response_verified(predicate)
    if err is not None:
        return err

    return VerifyResult(ok=True, code="OK", detail="", statement=statement)
