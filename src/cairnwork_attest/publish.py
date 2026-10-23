"""Publish a signed attestation bundle as a git note."""

from __future__ import annotations

import base64
import binascii
import json
import os
import random
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cairnwork_attest.bundle import BundleError, read_bundle
from cairnwork_attest.record import AttestError, parse_strict_json

_OID = re.compile(r"^[0-9a-f]{40}$")
_REMOTE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*$")
_NO_NOTE = "no note found"
_RETRYABLE = (
    "fetch first",
    "non-fast-forward",
    "cannot lock ref",
    "failed to update ref",
    "lock failure",
)
_RETRY = object()
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


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


def _git(repo: Path, *args: str, input: bytes | None = None) -> subprocess.CompletedProcess[str]:
    """Run git with prompts disabled and C-locale messages (stderr is matched)."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "LANGUAGE": "C"}
    proc = subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, check=False, input=input
    )
    return subprocess.CompletedProcess(
        proc.args,
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


# userinfo in a URL (https://user:token@host, https://token@host) that git may echo on stderr
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/@\s]+@")


def _redact(text: str) -> str:
    """Hide credentials embedded in remote URLs before git output reaches a result."""
    return _URL_USERINFO.sub(r"\1***@", text)


class _GitFailure(Exception):
    def __init__(self, code: str, detail: str) -> None:
        detail = _redact(detail)
        super().__init__(detail)
        self.code = code
        self.detail = detail


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


def _check_args(
    run: Runner, repo: Path, candidate: str, remote: str, notes_ref: str, attempts: int
) -> str | None:
    """Return a USAGE detail for unsafe or malformed arguments, else None."""
    if not _OID.fullmatch(candidate):
        return f"malformed candidate: {candidate!r} (must be 40-hex)"
    if not _REMOTE.fullmatch(remote):
        return f"remote must be a configured remote name: {remote!r}"
    if not notes_ref.startswith("refs/notes/") or notes_ref == "refs/notes/":
        return f"notes ref must be under refs/notes/: {notes_ref!r}"
    if run(repo, "check-ref-format", notes_ref).returncode != 0:
        return f"invalid notes ref: {notes_ref!r}"
    if attempts < 1:
        return f"max attempts must be >= 1: {attempts}"
    return None


def _read_and_check_bundle(bundle: Path, candidate: str) -> bytes:
    """Validate the bundle and return the exact bytes that were validated."""
    envelope = read_bundle(bundle)
    try:
        raw = bundle.read_bytes()
        again = parse_strict_json(
            raw.decode("utf-8"),
            error_cls=BundleError,
            malformed_code="BUNDLE_MALFORMED",
            constant_code="BUNDLE_MALFORMED",
        )
        raw_payload = base64.b64decode(envelope["payload"], validate=True)
        statement = json.loads(raw_payload)
        subject = statement["subject"][0]["digest"]["gitCommit"]
    except (OSError, KeyError, IndexError, TypeError, ValueError, binascii.Error) as exc:
        raise BundleError(f"statement shape: {exc}", code="STATEMENT_SHAPE") from exc
    if again != envelope:
        raise BundleError("bundle changed while being read", code="BUNDLE_CHANGED")
    if subject != candidate:
        raise BundleError(
            f"bundle subject gitCommit does not match candidate {candidate}",
            code="CANDIDATE_MISMATCH",
        )
    return raw


def _store_blob(run: Runner, repo: Path, raw: bytes) -> str:
    """Store the validated bytes as a git blob (no filters) and return the oid."""
    proc = run(repo, "hash-object", "-w", "--no-filters", "--stdin", input=raw)
    if proc.returncode != 0:
        raise _GitFailure("BLOB_STORE_FAILED", proc.stderr.strip())
    return proc.stdout.strip()


def _rev_parse(run: Runner, repo: Path, ref: str) -> str:
    proc = run(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if proc.returncode != 0 or not _OID.fullmatch(proc.stdout.strip()):
        raise _GitFailure("REV_PARSE_FAILED", proc.stderr.strip() or f"cannot resolve {ref}")
    return proc.stdout.strip()


def _fetch_notes(run: Runner, repo: Path, remote: str, notes_ref: str) -> None:
    """Mirror the remote notes ref locally (local-only notes on it are discarded)."""
    proc = run(repo, "fetch", "--no-tags", "--", remote, f"+{notes_ref}:{notes_ref}")
    if proc.returncode == 0:
        return
    if "couldn't find remote ref" not in proc.stderr:
        raise _GitFailure("FETCH_FAILED", proc.stderr.strip())
    exists = run(repo, "rev-parse", "--verify", "--quiet", notes_ref).returncode == 0
    if exists:
        deleted = run(repo, "update-ref", "-d", notes_ref)
        if deleted.returncode != 0:
            raise _GitFailure("LOCAL_REF_RESET_FAILED", deleted.stderr.strip())


def _existing_note(run: Runner, repo: Path, notes_ref: str, candidate: str) -> str | None:
    """Return the blob oid of the candidate's note, None if it has none."""
    proc = run(repo, "notes", f"--ref={notes_ref}", "list", candidate)
    if proc.returncode == 0:
        return proc.stdout.strip()
    if _NO_NOTE in proc.stderr.lower():
        return None
    raise _GitFailure("NOTES_LIST_FAILED", proc.stderr.strip())


def _restore_ref(run: Runner, repo: Path, notes_ref: str, old_tip: str | None) -> None:
    """Undo our local note commit so the local ref never shows an unpublished note."""
    if old_tip is None:
        proc = run(repo, "update-ref", "-d", notes_ref)
    else:
        proc = run(repo, "update-ref", notes_ref, old_tip)
    if proc.returncode != 0:
        raise _GitFailure("LOCAL_REF_RESET_FAILED", proc.stderr.strip())


def _try_push(
    run: Runner, repo: Path, remote: str, notes_ref: str, candidate: str, blob: str
) -> str | None | object:
    """Add the note and push. Return the tip if readable, or _RETRY when another push won."""
    probe = run(repo, "rev-parse", "--verify", "--quiet", notes_ref)
    old_tip = probe.stdout.strip() if probe.returncode == 0 else None
    added = run(repo, "notes", f"--ref={notes_ref}", "add", "-C", blob, candidate)
    if added.returncode != 0:
        raise _GitFailure("NOTE_ADD_FAILED", added.stderr.strip())
    proc = run(repo, "push", "--", remote, f"{notes_ref}:{notes_ref}")
    if proc.returncode == 0:
        try:
            return _rev_parse(run, repo, notes_ref)
        except _GitFailure:
            # The push succeeded; the tip is optional result metadata.
            return None
    try:
        _restore_ref(run, repo, notes_ref, old_tip)
    except _GitFailure as exc:  # keep why the push failed next to why the restore did
        raise _GitFailure(exc.code, f"{exc.detail}; push: {proc.stderr.strip()}") from None
    if "rejected" in proc.stderr and any(m in proc.stderr for m in _RETRYABLE):
        return _RETRY
    raise _GitFailure("PUSH_FAILED", proc.stderr.strip())


def _backoff(attempt: int) -> None:
    """Jittered wait so racing publishers do not retry in lockstep."""
    time.sleep(random.uniform(0.05, 0.25) * attempt)  # noqa: S311 - jitter, not crypto


def publish_bundle(
    bundle: Path,
    repo: Path,
    candidate: str,
    *,
    remote: str = "origin",
    notes_ref: str = "refs/notes/cairnwork",
    max_attempts: int = 5,
    run: Runner | None = None,
) -> PublishResult:
    """Publish a signed bundle as a git note on ``notes_ref`` and push it to ``remote``."""
    git = run or _git
    usage = _check_args(git, repo, candidate, remote, notes_ref, max_attempts)
    if usage is not None:
        return _fail("USAGE", usage, candidate, notes_ref, remote, 0)
    try:
        raw = _read_and_check_bundle(bundle, candidate)
    except AttestError as exc:
        return _fail(exc.code, exc.message, candidate, notes_ref, remote, 0)
    if git(repo, "cat-file", "-e", f"{candidate}^{{commit}}").returncode != 0:
        detail = f"commit {candidate} not in repo"
        return _fail("CANDIDATE_NOT_FOUND", detail, candidate, notes_ref, remote, 0)
    blob: str | None = None
    attempt = 0
    try:
        blob = _store_blob(git, repo, raw)
        for attempt in range(1, max_attempts + 1):
            _fetch_notes(git, repo, remote, notes_ref)
            existing = _existing_note(git, repo, notes_ref, candidate)
            if existing is not None and existing != blob:
                detail = f"existing note {existing} differs from {blob}"
                return _fail("NOTE_CONFLICT", detail, candidate, notes_ref, remote, attempt, blob)
            if existing == blob:
                tip = _rev_parse(git, repo, notes_ref)
                return _ok("NOOP", candidate, notes_ref, remote, attempt, blob, tip)
            tip = _try_push(git, repo, remote, notes_ref, candidate, blob)
            if tip is None or isinstance(tip, str):
                return _ok("PUBLISHED", candidate, notes_ref, remote, attempt, blob, tip)
            if attempt < max_attempts:
                _backoff(attempt)
    except _GitFailure as exc:
        return _fail(exc.code, exc.detail, candidate, notes_ref, remote, attempt, blob)
    detail = f"failed after {max_attempts} attempts"
    return _fail("PUSH_RETRIES_EXHAUSTED", detail, candidate, notes_ref, remote, max_attempts, blob)


def _ok(
    code: str,
    candidate: str,
    notes_ref: str,
    remote: str,
    attempts: int,
    blob: str,
    tip: str | None,
) -> PublishResult:
    return PublishResult(
        ok=True,
        code=code,
        detail=None,
        candidate=candidate,
        notes_ref=notes_ref,
        remote=remote,
        attempts=attempts,
        blob=blob,
        notes_tip=tip,
    )
