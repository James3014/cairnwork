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
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from http.client import HTTPException
from pathlib import Path
from typing import Any

from cairnwork_attest.bundle import BundleError, read_bundle
from cairnwork_attest.checkrun_gh import gh_http, gh_problem
from cairnwork_attest.dsse import verify_envelope
from cairnwork_attest.keys import load_public_key_pem
from cairnwork_attest.record import AttestError
from cairnwork_attest.verify import _parse_statement_payload, verify_bundle

_OID = re.compile(r"^[0-9a-f]{40}$")
_KEYID = re.compile(r"^[0-9a-f]{64}$")
_TEST_SOURCE = re.compile(r"^[a-z_]{1,32}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_.-]{1,512}$")
_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_UNSAFE = re.compile(r"[^A-Za-z0-9 ._:/+=,-]")
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
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise BundleError(f"payload is not base64: {exc}", code="STATEMENT_SHAPE") from exc
    statement, err = _parse_statement_payload(raw_payload)
    if err is not None or statement is None:
        raise BundleError(err.detail if err else "statement malformed", code="STATEMENT_SHAPE")
    try:
        candidate = statement["subject"][0]["digest"]["gitCommit"]
        keyid = envelope["signatures"][0].get("keyid")
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise BundleError(f"statement shape: {exc}", code="STATEMENT_SHAPE") from exc
    if not isinstance(candidate, str) or not _OID.fullmatch(candidate):
        raise BundleError("bundle subject gitCommit is not 40-hex", code="STATEMENT_SHAPE")
    predicate = statement["predicate"]
    return {
        "candidate": candidate,
        "base": _shown(predicate.get("base_commit"), _OID, "not present"),
        "test_source": _shown(predicate.get("test_source"), _TEST_SOURCE, "unknown"),
        "keyid": _shown(keyid, _KEYID, "not present"),
        "statement_sha256": hashlib.sha256(raw_payload).hexdigest(),
        "envelope": envelope,
    }


def _shown(value: object, pattern: re.Pattern[str], missing: str) -> str:
    """Show a bundle value only when it has the expected shape; never echo free text."""
    if value is None:
        return missing
    return value if isinstance(value, str) and pattern.fullmatch(value) else "invalid"


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
    key = load_public_key_pem(public_key.read_bytes())
    outcome = verify_envelope(facts["envelope"], key)
    return "VERIFIED, signature only" if outcome.ok else "SIGNATURE_INVALID"


def build_summary(facts: dict[str, Any], offline: str) -> str:
    """Render the markdown summary; every bundle-derived value is cleaned."""
    rows = [
        ("Candidate", facts["candidate"]),
        ("Base", facts["base"]),
        ("Statement SHA-256", facts["statement_sha256"]),
        ("Test source", facts["test_source"]),
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


def _http_error(status: int, body: bytes, token: str) -> CheckRunResult:
    message = ""
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            raw = str(parsed.get("message", ""))
            message = _clean(raw.replace(token, "[redacted]") if token else raw)
    except (ValueError, RecursionError):
        pass
    return _fail("HTTP_ERROR", f"HTTP {status}: {message}".rstrip(": "))


def _check_args(
    repo_slug: str,
    details_url: str | None,
    token: str | None,
    public_key: Path | None,
    repo: Path | None,
    gh: Path | None = None,
) -> str | None:
    if repo is not None and public_key is None:
        return "--repo needs --public-key (full offline verify)"
    if gh is not None:
        problem = gh_problem(gh)
        if problem is not None:
            return problem
    elif not token:
        return f"missing token (set {TOKEN_ENV})"
    elif not _TOKEN.fullmatch(token):
        return f"{TOKEN_ENV} has unexpected characters"
    if not _SLUG.fullmatch(repo_slug) or any(p in (".", "..") for p in repo_slug.split("/")):
        return f"repo slug must be OWNER/NAME: {_clean(repo_slug)!r}"
    if details_url is not None and not _https_url(details_url):
        return "details url must be https:// with a host and no whitespace"
    return None


def _https_url(url: str) -> bool:
    if not url.startswith("https://") or not re.fullmatch(r"[^\s\x00-\x1f\x7f]+", url):
        return False
    try:
        return bool(urllib.parse.urlsplit(url).hostname)
    except ValueError:
        return False


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
    gh: Path | None = None,
) -> CheckRunResult:
    """Post a neutral Check Run on the bundle's candidate commit.

    Never raises for bad input. With ``public_key`` the signature is checked
    offline (full ``verify_bundle`` when ``repo`` is also given). The token is
    only placed in the Authorization header, never in any result. With ``gh``
    (a ``gh``-compatible CLI that authenticates itself) no token is used at all.
    """
    usage = _check_args(repo_slug, details_url, token, public_key, repo, gh)
    if usage is not None:
        return _fail("USAGE", usage)
    try:
        facts = _facts(Path(bundle_path))
        if public_key is not None:
            load_public_key_pem(
                public_key.read_bytes()
            )  # an operator key problem never reads as a forgery
    except AttestError as exc:
        return _fail(exc.code, _clean(exc.message))
    except OSError as exc:
        return _fail("KEY_UNREADABLE", _clean(exc.strerror or "cannot read public key"))
    candidate = facts["candidate"]
    facts["bundle"] = Path(bundle_path)
    offline = _offline_result(facts, public_key, repo)
    title = "Cairnwork attestation (informational)"
    if public_key is None:
        title = "Cairnwork attestation (informational, UNVERIFIED: signature not checked)"
    output = {"title": title, "summary": ""}
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
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "cairnwork-attest",
    }
    if gh is None:
        headers["Authorization"] = f"Bearer {token}"
    else:
        http, token = gh_http(gh), None
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    url = f"{_API}/repos/{repo_slug}/check-runs"
    try:
        status, resp = (http or _default_http)(url, "POST", headers, body)
    except (OSError, urllib.error.URLError, TimeoutError, HTTPException):
        return _fail("NETWORK_ERROR", "request to GitHub failed", candidate)
    if status != 201:
        failed = _http_error(status, resp, token or "")
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
    p.add_argument("--gh", type=Path, default=None, help="gh-compatible CLI that authenticates")
    p.add_argument("--json", action="store_true", help="JSON output")


def cmd_check_run(args: argparse.Namespace) -> int:
    """Dispatch the check-run subcommand (token from the environment only, or --gh)."""
    result = post_check_run(
        args.bundle,
        args.repo_slug,
        public_key=args.public_key,
        repo=args.repo,
        name=args.name,
        details_url=args.details_url,
        token=os.environ.get(TOKEN_ENV),
        gh=args.gh,
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
        print(f"CREATED {result.candidate} {result.html_url or '-'}")
    else:
        detail = f": {result.detail}" if result.detail else ""
        print(f"{result.code}{detail}", file=sys.stderr)
    return 0 if result.ok else 2
