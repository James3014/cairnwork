"""Publish a signed attestation bundle as a git note."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cairnwork_attest.bundle import BundleError, read_bundle
from cairnwork_attest.record import AttestError

_OID = re.compile(r"^[0-9a-f]{40}$")
_RETRY = object()


@dataclass(frozen=True)
class PublishResult:
    """Outcome of a publish attempt."""

    ok: bool
    code: str
    detail: str | None
    candidate: str
    notes_ref: str
    remote: str
    attempts: int
    blob: str | None
    notes_tip: str | None


def _git(repo: Path, *args: str, input: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run a git command with prompts disabled."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        input=input,
    )


def _fail(
    code: str,
    detail: str | None,
    candidate: str,
    notes_ref: str,
    remote: str,
    attempts: int,
    blob: str | None = None,
) -> PublishResult:
    """Build a failure result."""
    return PublishResult(
        ok=False,
        code=code,
        detail=detail,
        candidate=candidate,
        notes_ref=notes_ref,
        remote=remote,
        attempts=attempts,
        blob=blob,
        notes_tip=None,
    )


def _read_and_check_bundle(bundle: Path, candidate: str) -> dict[str, Any]:
    """Read and validate the bundle; its statement subject must name the candidate."""
    envelope = read_bundle(bundle)
    try:
        raw_payload = base64.b64decode(envelope["payload"], validate=True)
        statement = json.loads(raw_payload)
        subject = statement["subject"][0]["digest"]["gitCommit"]
    except (KeyError, IndexError, TypeError, ValueError, binascii.Error) as exc:
        raise BundleError(f"statement shape: {exc}", code="STATEMENT_SHAPE") from exc
    if subject != candidate:
        raise BundleError(
            f"bundle subject gitCommit does not match candidate {candidate}",
            code="CANDIDATE_MISMATCH",
        )
    return envelope


def _store_blob(run: Any, repo: Path, bundle: Path) -> str:
    """Store the bundle file's exact bytes as a git blob and return the oid.

    The file is hashed by path with ``--no-filters`` so no clean filter or
    end-of-line conversion touches it; the note is byte-identical to the file.
    """
    proc = run(repo, "hash-object", "-w", "--no-filters", "--", str(bundle.resolve()))
    if proc.returncode != 0:
        detail = f"git hash-object failed: {proc.stderr.strip()}"
        raise BundleError(detail, code="BLOB_STORE_FAILED")
    return proc.stdout.strip()


def _fetch_notes(
    run: Any,
    repo: Path,
    remote: str,
    notes_ref: str,
) -> str | None:
    """Fetch remote notes ref. Return None on success, or error detail."""
    proc = run(repo, "fetch", remote, f"+{notes_ref}:{notes_ref}")
    if proc.returncode == 0:
        return None
    if "couldn't find remote ref" in proc.stderr:
        run(repo, "update-ref", "-d", notes_ref)
        return None
    return proc.stderr.strip()


def _check_existing_note(
    run: Any,
    repo: Path,
    notes_ref: str,
    candidate: str,
    blob_oid: str,
) -> PublishResult | None:
    """Check for an existing note. Return a result for NOOP/CONFLICT, or None."""
    proc = run(repo, "notes", "--ref", notes_ref, "list", candidate)
    if proc.returncode != 0:
        return None
    existing_oid = proc.stdout.strip()
    if existing_oid == blob_oid:
        tip = run(repo, "rev-parse", notes_ref).stdout.strip()
        return PublishResult(
            ok=True,
            code="NOOP",
            detail=None,
            candidate=candidate,
            notes_ref=notes_ref,
            remote="",
            attempts=0,
            blob=blob_oid,
            notes_tip=tip,
        )
    return PublishResult(
        ok=False,
        code="NOTE_CONFLICT",
        detail=f"existing note {existing_oid} differs from {blob_oid}",
        candidate=candidate,
        notes_ref=notes_ref,
        remote="",
        attempts=0,
        blob=blob_oid,
        notes_tip=None,
    )


def _try_push(
    run: Any,
    repo: Path,
    remote: str,
    notes_ref: str,
    candidate: str,
    blob_oid: str,
) -> PublishResult | object:
    """Attempt add + push. Return PublishResult, _RETRY, or a failure result."""
    added = run(repo, "notes", "--ref", notes_ref, "add", "-C", blob_oid, candidate)
    if added.returncode != 0:
        return _fail(
            "NOTE_ADD_FAILED", added.stderr.strip(), candidate, notes_ref, remote, 0, blob_oid
        )
    proc = run(repo, "push", remote, f"{notes_ref}:{notes_ref}")
    if proc.returncode == 0:
        tip = run(repo, "rev-parse", notes_ref).stdout.strip()
        return PublishResult(
            ok=True,
            code="PUBLISHED",
            detail=None,
            candidate=candidate,
            notes_ref=notes_ref,
            remote=remote,
            attempts=0,
            blob=blob_oid,
            notes_tip=tip,
        )
    stderr = proc.stderr
    if "rejected" in stderr and ("fetch first" in stderr or "non-fast-forward" in stderr):
        return _RETRY
    return PublishResult(
        ok=False,
        code="PUSH_FAILED",
        detail=stderr.strip(),
        candidate=candidate,
        notes_ref=notes_ref,
        remote=remote,
        attempts=0,
        blob=blob_oid,
        notes_tip=None,
    )


def publish_bundle(
    bundle: Path,
    repo: Path,
    candidate: str,
    *,
    remote: str = "origin",
    notes_ref: str = "refs/notes/cairnwork",
    max_attempts: int = 5,
    run: Any = None,
) -> PublishResult:
    """Publish a signed bundle as a git note and push to the remote."""
    if run is None:
        run = _git
    if not _OID.fullmatch(candidate):
        return _fail(
            "USAGE", f"malformed candidate: {candidate!r}", candidate, notes_ref, remote, 0
        )
    try:
        _read_and_check_bundle(bundle, candidate)
    except AttestError as exc:
        return _fail(exc.code, exc.message, candidate, notes_ref, remote, 0)
    proc = run(repo, "cat-file", "-e", f"{candidate}^{{commit}}")
    if proc.returncode != 0:
        return _fail(
            "CANDIDATE_NOT_FOUND",
            f"commit {candidate} not in repo",
            candidate,
            notes_ref,
            remote,
            0,
        )
    try:
        blob_oid = _store_blob(run, repo, bundle)
    except AttestError as exc:
        return _fail(exc.code, exc.message, candidate, notes_ref, remote, 0)
    for attempt in range(1, max_attempts + 1):
        err = _fetch_notes(run, repo, remote, notes_ref)
        if err is not None:
            return _fail("FETCH_FAILED", err, candidate, notes_ref, remote, attempt, blob_oid)
        existing = _check_existing_note(run, repo, notes_ref, candidate, blob_oid)
        if existing is not None:
            r = existing
            return PublishResult(
                ok=r.ok,
                code=r.code,
                detail=r.detail,
                candidate=candidate,
                notes_ref=notes_ref,
                remote=remote,
                attempts=attempt,
                blob=r.blob,
                notes_tip=r.notes_tip,
            )
        result = _try_push(run, repo, remote, notes_ref, candidate, blob_oid)
        if result is _RETRY:
            continue
        r = result  # type: ignore[assignment]
        return PublishResult(
            ok=r.ok,
            code=r.code,
            detail=r.detail,
            candidate=candidate,
            notes_ref=notes_ref,
            remote=remote,
            attempts=attempt,
            blob=r.blob,
            notes_tip=r.notes_tip,
        )
    return _fail(
        "PUSH_RETRIES_EXHAUSTED",
        f"failed after {max_attempts} attempts",
        candidate,
        notes_ref,
        remote,
        max_attempts,
        blob_oid,
    )
