"""One-shot runner hook: sign the statement, publish the note, post the Check Run.

Called after a task was verified. It runs ``statement --sign``, ``publish`` and
``check-run`` in order by calling their Python functions, stops at the first
failing step and prints exactly one JSON line. The token is read from the
environment only and never reaches any output.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cairnwork_attest.bundle import BundleError
from cairnwork_attest.checkrun import _SLUG, TOKEN_ENV, post_check_run
from cairnwork_attest.dsse import sign_statement
from cairnwork_attest.keys import load_private_key, public_key_pem
from cairnwork_attest.publish import publish_bundle
from cairnwork_attest.record import AttestError, VerifiedRecord, verify_record
from cairnwork_attest.statement import default_producer, run_test_source
from cairnwork_attest.verify import verify_bundle
from cairnwork_protocol import canonical_json

_OID = re.compile(r"^[0-9a-f]{40}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")  # same shape as the core's attempt id
_STEPS = ("statement", "publish", "check_run")
_PUBLISH_OK = ("PUBLISHED", "NOOP")
_NO_CHECK_RUN = "--no-check-run"


@dataclass(frozen=True)
class _Step:
    step: str
    code: str
    detail: str | None = None


def _usage(args: argparse.Namespace) -> str | None:
    """Return a reason when an input is malformed; nothing is touched before this passes."""
    for name, value in (("task-id", args.task_id), ("attempt-id", args.attempt_id)):
        if not _ID.fullmatch(value):
            return f"--{name} must match [A-Za-z0-9][A-Za-z0-9_-]{{0,63}}"
    if not _OID.fullmatch(args.candidate):
        return "--candidate must be 40 lowercase hex"
    if not _SLUG.fullmatch(args.repo_slug) or ".." in args.repo_slug.split("/"):
        return "--repo-slug must be OWNER/NAME"
    paths = {"state": args.state, "repo": args.repo, "out-dir": args.out_dir}
    for name, path in paths.items():
        if path is not None and not Path(path).is_absolute():
            return f"--{name} must be an absolute path"
    return None


def _build_envelope(args: argparse.Namespace, verified: VerifiedRecord) -> dict[str, Any]:
    """Build and sign the statement exactly as ``statement --sign`` does."""
    state_dir = Path(args.state)
    return sign_statement(
        verified,
        producer=default_producer(),
        timestamp=datetime.now(UTC).isoformat(),
        private_key=load_private_key(state_dir),
        test_source=run_test_source(state_dir, args.task_id, args.attempt_id),
    )


def _write_private(path: Path, envelope: dict[str, Any]) -> None:
    """Create the bundle exclusively with mode 0600."""
    data = canonical_json(envelope).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise BundleError(f"bundle already exists: {path}", code="BUNDLE_EXISTS") from exc
    except OSError as exc:
        raise BundleError(f"cannot create bundle file: {exc}", code="BUNDLE_UNWRITABLE") from exc
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    except OSError as exc:
        os.close(fd)
        path.unlink(missing_ok=True)
        raise BundleError(f"cannot write bundle: {exc}", code="BUNDLE_UNWRITABLE") from exc
    os.close(fd)


def _existing_is_ours(
    path: Path, args: argparse.Namespace, pubkey: Path, verified: VerifiedRecord
) -> bool:
    """Reuse a bundle left by an earlier run only if it fully verifies for this candidate and
    was signed for this very notary record (same request hash, so same task and attempt)."""
    result = verify_bundle(
        path, public_key_pem=pubkey, repo=Path(args.repo), expected_candidate=args.candidate
    )
    if not result.ok or result.statement is None:
        return False
    predicate = result.statement.get("predicate")
    return (
        isinstance(predicate, dict)
        and predicate.get("request_hash") == verified.record.request_hash
    )


def _statement_step(args: argparse.Namespace, bundle: Path, pubkey: Path) -> _Step:
    try:
        pubkey.write_text(public_key_pem(load_private_key(Path(args.state)).public_key()))
        verified = verify_record(
            Path(args.state) / "notary.sqlite",
            args.task_id,
            args.attempt_id,
            expected_candidate=args.candidate,
        )
        if bundle.is_symlink():
            raise BundleError(f"bundle path is a symlink: {bundle}", code="BUNDLE_EXISTS")
        if bundle.exists():
            if _existing_is_ours(bundle, args, pubkey, verified):
                return _Step("statement", "OK", "reused existing bundle")
            raise BundleError(f"bundle already exists: {bundle}", code="BUNDLE_EXISTS")
        bundle.parent.mkdir(parents=True, exist_ok=True)
        _write_private(bundle, _build_envelope(args, verified))
    except AttestError as exc:
        return _Step("statement", exc.code, exc.message)
    except (OSError, ValueError) as exc:
        return _Step(
            "statement", "ERROR", str(getattr(exc, "strerror", None) or "cannot build statement")
        )
    return _Step("statement", "OK")


def _publish_step(args: argparse.Namespace, bundle: Path) -> _Step:
    result = publish_bundle(
        bundle=bundle,
        repo=Path(args.repo),
        candidate=args.candidate,
        remote=args.remote,
        notes_ref=args.notes_ref,
    )
    return _Step("publish", result.code, result.detail)


def _check_run_step(args: argparse.Namespace, bundle: Path, pubkey: Path) -> _Step:
    result = post_check_run(
        bundle,
        args.repo_slug,
        public_key=pubkey,
        repo=Path(args.repo),
        token=os.environ.get(TOKEN_ENV),
    )
    return _Step("check_run", result.code, result.detail)


def _run_steps(args: argparse.Namespace, bundle: Path, pubkey: Path) -> list[_Step]:
    steps = [_statement_step(args, bundle, pubkey)]
    if steps[-1].code == "OK":
        steps.append(_publish_step(args, bundle))
    if steps[-1].step == "publish" and steps[-1].code in _PUBLISH_OK:
        if args.no_check_run:
            steps.append(_Step("check_run", "SKIPPED", _NO_CHECK_RUN))
        else:
            steps.append(_check_run_step(args, bundle, pubkey))
    return steps


def _succeeded(step: _Step) -> bool:
    if step.step == "check_run" and step.code == "SKIPPED":
        return step.detail == _NO_CHECK_RUN
    ok = {"statement": ("OK",), "publish": _PUBLISH_OK, "check_run": ("CREATED",)}
    return step.code in ok[step.step]


def _scrub(text: str | None) -> str | None:
    token = os.environ.get(TOKEN_ENV)
    if text is None or not token:
        return text
    return text.replace(token, "[redacted]")


def _report(args: argparse.Namespace, bundle: Path | None, steps: list[_Step]) -> int:
    done = {s.step for s in steps}
    steps = steps + [
        _Step(name, "SKIPPED", "an earlier step failed") for name in _STEPS if name not in done
    ]
    ok = all(_succeeded(s) for s in steps)
    obj = {
        "attempt_id": args.attempt_id,
        "bundle": str(bundle) if bundle is not None else None,
        "candidate": args.candidate,
        "ok": ok,
        "steps": [{"step": s.step, "code": s.code, "detail": _scrub(s.detail)} for s in steps],
        "task_id": args.task_id,
    }
    print(json.dumps(obj, sort_keys=True, separators=(",", ":")))
    if ok:
        return 0
    publish = next((s for s in steps if s.step == "publish"), None)
    return 1 if publish is not None and publish.code == "NOTE_CONFLICT" else 2


def add_subparser(subparsers: Any) -> None:
    """Register the ``on-verified`` subcommand."""
    p = subparsers.add_parser("on-verified", help="Sign, publish and report a verified task")
    p.add_argument("--state", type=Path, required=True, help="State directory (absolute)")
    p.add_argument("--task-id", type=str, required=True, help="Task ID")
    p.add_argument("--attempt-id", type=str, required=True, help="Attempt ID")
    p.add_argument("--candidate", type=str, required=True, help="Candidate commit SHA (40-hex)")
    p.add_argument("--repo", type=Path, required=True, help="Git clone directory (absolute)")
    p.add_argument("--repo-slug", type=str, required=True, help="GitHub OWNER/NAME")
    p.add_argument("--remote", type=str, default="origin", help="Git remote name")
    p.add_argument("--notes-ref", type=str, default="refs/notes/cairnwork", help="Notes ref path")
    p.add_argument("--out-dir", type=Path, default=None, help="Bundle directory (absolute)")
    p.add_argument("--no-check-run", action="store_true", help="Skip the informational Check Run")


def cmd_on_verified(args: argparse.Namespace) -> int:
    """Dispatch on-verified: always one JSON line; exit 0 ok, 1 note conflict, 2 other."""
    reason = _usage(args)
    if reason is not None:
        return _report(args, None, [_Step("statement", "USAGE", reason)])
    out_dir = Path(args.out_dir) if args.out_dir is not None else Path(args.state) / "attestations"
    bundle = out_dir / f"{args.task_id}-{args.attempt_id}.bundle.json"
    with tempfile.TemporaryDirectory(prefix="cairnwork-onverified-") as tmp:
        steps = _run_steps(args, bundle, Path(tmp) / "public.pem")
    return _report(args, bundle if steps[0].code == "OK" else None, steps)
