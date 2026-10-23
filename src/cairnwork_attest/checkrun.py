"""Post an informational GitHub Check Run for a published attestation bundle.

The Check Run is never the enforcing gate: its conclusion is always ``neutral``.
Enforcement is done by cairnwork/verify-action, which verifies the bundle itself.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cairnwork_attest.bundle import BundleError, read_bundle
from cairnwork_attest.dsse import verify_envelope
from cairnwork_attest.keys import KeyFileError, load_public_key_pem
from cairnwork_attest.record import AttestError
from cairnwork_attest.verify import verify_bundle

_OID = re.compile(r"^[0-9a-f]{40}$")
_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_UNSAFE = re.compile(r"[^A-Za-z0-9 ._:/@+=,-]")
_MAX_VALUE = 200
_TIMEOUT_SECONDS = 30
_API = "https://api.github.com"
TOKEN_ENV = "CAIRNWORK_GITHUB_TOKEN"
Http = Callable[[str, str, dict[str, str], bytes], tuple[int, bytes]]


@dataclass(frozen=True)
class CheckRunResult:
    """Outcome of a Check Run post."""

    ok: bool
    code: str
    detail: str | None
    candidate: str | None
    check_run_id: int | None
    html_url: str | None


def _fail(code: str, detail: str, candidate: str | None = None) -> CheckRunResult:
    return CheckRunResult(False, code, detail, candidate, None, None)


def _clean(value: object) -> str:
    """Make an untrusted value plain text: only a conservative charset, capped length."""
    return _UNSAFE.sub("?", str(value))[:_MAX_VALUE]


def _default_http(url: str, method: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as resp:  # noqa: S310
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _facts(bundle_path: Path) -> dict[str, Any]:
    """Read the bundle with the existing strict reader and extract its claimed facts.

    ``statement_sha256`` is the SHA-256 of the decoded DSSE payload (the exact
    Statement bytes that were signed), the same digest ``cairnwork-attest verify
    --json`` reports as ``statement_sha256``.
    """
    envelope = read_bundle(bundle_path)
    try:
        raw_payload = base64.b64decode(envelope["payload"], validate=True)
        statement = json.loads(raw_payload)
        candidate = statement["subject"][0]["digest"]["gitCommit"]
        predicate = statement.get("predicate", {})
        keyid = envelope["signatures"][0]["keyid"]
    except (KeyError, IndexError, TypeError, ValueError, AttributeError, binascii.Error) as exc:
        raise BundleError(f"statement shape: {exc}", code="STATEMENT_SHAPE") from exc
    if not isinstance(candidate, str) or not _OID.fullmatch(candidate):
        raise BundleError("bundle subject gitCommit is not 40-hex", code="STATEMENT_SHAPE")
    if not isinstance(predicate, dict):
        raise BundleError("statement predicate is not an object", code="STATEMENT_SHAPE")
    return {
        "candidate": candidate,
        "base": predicate.get("base_commit"),
        "test_source": predicate.get("test_source"),
        "keyid": keyid,
        "statement_sha256": hashlib.sha256(raw_payload).hexdigest(),
        "envelope": envelope,
    }


def _offline_result(facts: dict[str, Any], public_key: Path | None, repo: Path | None) -> str:
    """Return VERIFIED, a failure code, or a note that the signature was not checked."""
    if public_key is None:
        return "signature not checked by this step"
    if repo is not None:
        result = verify_bundle(
            Path(facts["bundle"]),
            public_key_pem=public_key,
            repo=repo,
            expected_candidate=facts["candidate"],
        )
        return "VERIFIED" if result.ok else result.code
    try:
        key = load_public_key_pem(public_key.read_bytes())
    except (KeyFileError, OSError):
        return "SIGNATURE_INVALID"
    outcome = verify_envelope(facts["envelope"], key)
    return "VERIFIED, signature only" if outcome.ok else "SIGNATURE_INVALID"


def build_summary(facts: dict[str, Any], offline: str) -> str:
    """Render the markdown summary; every bundle-derived value is cleaned."""
    rows = [
        ("Candidate", facts["candidate"]),
        ("Base", facts["base"] if facts["base"] is not None else "not present"),
        ("Statement SHA-256", facts["statement_sha256"]),
        ("Test source", facts["test_source"] if facts["test_source"] is not None else "unknown"),
        ("Key id", facts["keyid"]),
        ("Offline verification", offline),
    ]
    lines = ["Informational attestation report. Not an enforcing check.", ""]
    lines += [f"- {label}: {_clean(value)}" for label, value in rows]
    lines += [
        "",
        "Verify offline with `cairnwork-attest verify`. "
        "The enforcing check is cairnwork/verify-action.",
    ]
    return "\n".join(lines)


def _http_error(status: int, body: bytes) -> CheckRunResult:
    message = ""
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            message = _clean(parsed.get("message", ""))
    except ValueError:
        pass
    return _fail("HTTP_ERROR", f"HTTP {status}: {message}".rstrip(": "))


def _check_args(repo_slug: str, details_url: str | None, token: str | None) -> str | None:
    if not token:
        return f"missing token (set {TOKEN_ENV})"
    if not _SLUG.fullmatch(repo_slug) or any(p in (".", "..") for p in repo_slug.split("/")):
        return f"repo slug must be OWNER/NAME: {_clean(repo_slug)!r}"
    if details_url is not None and not (
        details_url.startswith("https://") and re.fullmatch(r"[^\s\x00-\x1f\x7f]+", details_url)
    ):
        return "details url must start with https:// and contain no whitespace"
    return None


def post_check_run(
    bundle_path: Path,
    repo_slug: str,
    *,
    public_key: Path | None = None,
    repo: Path | None = None,
    name: str = "cairnwork attestation",
    details_url: str | None = None,
    token: str | None,
    http: Http | None = None,
) -> CheckRunResult:
    """Post a neutral Check Run on the bundle's candidate commit.

    Never raises for bad input. With ``public_key`` the signature is checked
    offline (full ``verify_bundle`` when ``repo`` is also given). The token is
    only placed in the Authorization header, never in any result.
    """
    usage = _check_args(repo_slug, details_url, token)
    if usage is not None:
        return _fail("USAGE", usage)
    try:
        facts = _facts(Path(bundle_path))
    except AttestError as exc:
        return _fail(exc.code, _clean(exc.message))
    candidate = facts["candidate"]
    facts["bundle"] = Path(bundle_path)
    offline = _offline_result(facts, public_key, repo)
    output = {"title": "Cairnwork attestation (informational)", "summary": ""}
    output["summary"] = build_summary(facts, offline)
    payload: dict[str, Any] = {
        "name": _clean(name),
        "head_sha": candidate,
        "status": "completed",
        "conclusion": "neutral",
        "output": output,
    }
    if details_url is not None:
        payload["details_url"] = details_url
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "cairnwork-attest",
    }
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    url = f"{_API}/repos/{repo_slug}/check-runs"
    try:
        status, resp = (http or _default_http)(url, "POST", headers, body)
    except (OSError, urllib.error.URLError, TimeoutError):
        return _fail("NETWORK_ERROR", "request to GitHub failed", candidate)
    if status != 201:
        failed = _http_error(status, resp)
        return _fail(failed.code, failed.detail or "", candidate)
    try:
        created = json.loads(resp)
        run_id, html_url = created.get("id"), created.get("html_url")
    except (ValueError, AttributeError):
        run_id = html_url = None
    run_id = run_id if isinstance(run_id, int) else None
    html_url = _clean(html_url) if isinstance(html_url, str) else None
    return CheckRunResult(True, "CREATED", None, candidate, run_id, html_url)


def add_subparser(subparsers: Any) -> None:
    """Register the ``check-run`` subcommand."""
    p = subparsers.add_parser("check-run", help="Post an informational GitHub Check Run")
    p.add_argument("--bundle", type=Path, required=True, help="Signed bundle file path")
    p.add_argument("--repo-slug", type=str, required=True, help="GitHub OWNER/NAME")
    p.add_argument("--public-key", type=Path, default=None, help="Public key PEM to check")
    p.add_argument("--repo", type=Path, default=None, help="Git repo for full offline verify")
    p.add_argument("--name", type=str, default="cairnwork attestation", help="Check Run name")
    p.add_argument("--details-url", type=str, default=None, help="https:// details URL")
    p.add_argument("--json", action="store_true", help="JSON output")


def cmd_check_run(args: argparse.Namespace) -> int:
    """Dispatch the check-run subcommand (token comes from the environment only)."""
    result = post_check_run(
        args.bundle,
        args.repo_slug,
        public_key=args.public_key,
        repo=args.repo,
        name=args.name,
        details_url=args.details_url,
        token=os.environ.get(TOKEN_ENV),
    )
    if args.json:
        obj = {
            "candidate": result.candidate,
            "check_run_id": result.check_run_id,
            "code": result.code,
            "detail": result.detail,
            "html_url": result.html_url,
            "ok": result.ok,
        }
        print(json.dumps(obj, sort_keys=True, separators=(",", ":")))
    elif result.ok:
        print(f"CREATED {result.candidate} {result.html_url}")
    else:
        detail = f": {result.detail}" if result.detail else ""
        print(f"{result.code}{detail}", file=sys.stderr)
    return 0 if result.ok else 2
