"""Offline bundle writer and verifier CLI for Cairnwork attestations."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cairnwork_attest.bundle import BundleError, write_bundle
from cairnwork_attest.checkrun import add_subparser as _add_check_run
from cairnwork_attest.checkrun import cmd_check_run
from cairnwork_attest.dsse import sign_statement
from cairnwork_attest.keys import (
    KeyFileError,
    generate_keypair,
    keyid,
    load_private_key,
    public_key_pem,
)
from cairnwork_attest.on_verified import add_subparser as _add_on_verified
from cairnwork_attest.on_verified import cmd_on_verified
from cairnwork_attest.publish import publish_bundle
from cairnwork_attest.record import AttestError, parse_strict_json, verify_record
from cairnwork_attest.statement import (
    TEST_SOURCES,
    build_statement,
    default_producer,
    run_test_source,
)
from cairnwork_attest.verify import verify_bundle
from cairnwork_protocol import canonical_json

_OID = re.compile(r"^[0-9a-f]{40}$")


def _cmd_keygen(args: argparse.Namespace) -> int:
    try:
        pubkey = generate_keypair(Path(args.state))
    except KeyFileError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"keyid: {keyid(pubkey)}")
    print(public_key_pem(pubkey), end="")
    return 0


def _write_unsigned_statement(path: Path, statement: dict[str, Any]) -> None:
    data = canonical_json(statement).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o644)
    except FileExistsError as exc:
        raise BundleError(f"file already exists: {path}", code="FILE_EXISTS") from exc
    except OSError as exc:
        raise BundleError(f"cannot create file: {exc}", code="FILE_UNWRITABLE") from exc

    try:
        os.fchmod(fd, 0o644)
        os.write(fd, data)
        os.fsync(fd)
    except OSError as exc:
        os.close(fd)
        path.unlink(missing_ok=True)
        raise BundleError(f"cannot write statement: {exc}", code="FILE_UNWRITABLE") from exc
    os.close(fd)


def _cmd_statement(args: argparse.Namespace) -> int:
    state_dir = Path(args.state)
    notary_sqlite = state_dir / "notary.sqlite"

    try:
        verified = verify_record(
            notary_sqlite,
            args.task_id,
            args.attempt_id,
            expected_candidate=args.candidate,
        )
        producer = default_producer()
        timestamp = datetime.now(UTC).isoformat()
        test_source = run_test_source(state_dir, args.task_id, args.attempt_id)

        out_path = Path(args.out)
        if args.sign:
            private_key = load_private_key(state_dir)
            envelope = sign_statement(
                verified,
                producer=producer,
                timestamp=timestamp,
                private_key=private_key,
                test_source=test_source,
            )
            write_bundle(out_path, envelope)
        else:
            stmt = build_statement(
                verified, producer=producer, timestamp=timestamp, test_source=test_source
            )
            _write_unsigned_statement(out_path, stmt)
    except AttestError as exc:
        print(exc.code, file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    return 0


def _bundle_facts(path: Path) -> dict[str, Any]:
    nulls: dict[str, Any] = {
        "base": None,
        "statement_sha256": None,
        "core_status": None,
        "keyid": None,
        "test_source": None,
    }
    try:
        content = path.read_bytes()
        envelope = json.loads(content)
        raw_payload = base64.b64decode(envelope["payload"], validate=True)
        statement = json.loads(raw_payload)
        base = None
        core_status = None
        keyid_val = None
        source = None
        if isinstance(envelope, dict):
            sigs = envelope.get("signatures")
            if isinstance(sigs, list) and sigs and isinstance(sigs[0], dict):
                k = sigs[0].get("keyid")
                if isinstance(k, str):
                    keyid_val = k
        if isinstance(statement, dict):
            pred = statement.get("predicate")
            if isinstance(pred, dict):
                b = pred.get("base_commit")
                if isinstance(b, str) and _OID.fullmatch(b):
                    base = b
                st = pred.get("status")
                if isinstance(st, str):
                    core_status = st
                if pred.get("test_source") in TEST_SOURCES:
                    source = pred["test_source"]
        return {
            "test_source": source,
            "base": base,
            "statement_sha256": hashlib.sha256(raw_payload).hexdigest(),
            "core_status": core_status,
            "keyid": keyid_val,
        }
    except (OSError, ValueError, UnicodeDecodeError, KeyError, TypeError, binascii.Error):
        return nulls


def _print_json(
    *,
    ok: bool,
    code: str,
    detail: str | None,
    candidate: str,
    base: str | None = None,
    statement_sha256: str | None = None,
    core_status: str | None = None,
    keyid: str | None = None,
    test_source: str | None = None,
) -> None:
    obj = {
        "base": base,
        "candidate": candidate,
        "code": code,
        "core_status": core_status,
        "detail": detail,
        "keyid": keyid,
        "ok": ok,
        "statement_sha256": statement_sha256,
        "test_source": test_source,
    }
    print(json.dumps(obj, sort_keys=True, separators=(",", ":")))


def _usage_error(msg: str, candidate: str, as_json: bool) -> int:
    print(f"ERROR: {msg}", file=sys.stderr)
    if as_json:
        _print_json(ok=False, code="USAGE", detail=msg, candidate=candidate)
    return 2


def _cmd_verify(args: argparse.Namespace) -> int:
    candidate = args.candidate
    if not _OID.fullmatch(candidate):
        return _usage_error(
            f"malformed --candidate: {candidate!r} (must be 40-hex)", candidate, args.json
        )

    expected_base = args.base
    if expected_base is not None and not _OID.fullmatch(expected_base):
        return _usage_error(
            f"malformed --base: {expected_base!r} (must be 40-hex)", candidate, args.json
        )

    repo = Path(args.repo)
    if not args.json:
        print(f"Candidate: {candidate}", file=sys.stderr)

    core_id_pin: Mapping[str, Any] | None = None
    if args.core_identity is not None:
        try:
            core_id_text = Path(args.core_identity).read_text(encoding="utf-8")
            core_id_pin = parse_strict_json(
                core_id_text,
                error_cls=BundleError,
                malformed_code="CORE_IDENTITY_MALFORMED",
                constant_code="CORE_IDENTITY_MALFORMED",
            )
            if not isinstance(core_id_pin, Mapping):
                return _usage_error(
                    "core-identity must decode to a JSON object", candidate, args.json
                )
        except (BundleError, OSError, UnicodeDecodeError) as exc:
            return _usage_error(f"cannot read core-identity: {exc}", candidate, args.json)

    result = verify_bundle(
        Path(args.bundle),
        public_key_pem=Path(args.public_key),
        repo=repo,
        expected_candidate=candidate,
        core_identity_pin=core_id_pin,
        expected_base=expected_base,
    )

    if args.json:
        facts = _bundle_facts(Path(args.bundle))
        _print_json(
            ok=result.ok,
            code="OK" if result.ok else result.code,
            detail=result.detail or None,
            candidate=candidate,
            base=facts["base"],
            statement_sha256=facts["statement_sha256"],
            core_status=facts["core_status"],
            keyid=facts["keyid"],
            test_source=facts["test_source"],
        )
        return 0 if result.ok else 1

    if result.ok and result.statement is not None:
        cand = result.statement["subject"][0]["digest"]["gitCommit"]
        print(f"OK {cand}")
        return 0

    detail = f": {result.detail}" if result.detail else ""
    print(f"{result.code}{detail}")
    return 1


def _cmd_publish(args: argparse.Namespace) -> int:
    """Dispatch the publish subcommand."""
    result = publish_bundle(
        bundle=Path(args.bundle),
        repo=Path(args.repo),
        candidate=args.candidate,
        remote=args.remote,
        notes_ref=args.notes_ref,
        max_attempts=args.max_attempts,
    )
    if args.json:
        obj = {
            "attempts": result.attempts,
            "blob": result.blob,
            "candidate": result.candidate,
            "code": result.code,
            "detail": result.detail,
            "notes_ref": result.notes_ref,
            "notes_tip": result.notes_tip,
            "ok": result.ok,
            "remote": result.remote,
        }
        print(json.dumps(obj, sort_keys=True, separators=(",", ":")))
    elif result.ok:
        if result.code == "NOOP":
            print(f"NOOP {result.candidate}")
        else:
            print(f"PUBLISHED {result.candidate} {result.notes_ref} {result.notes_tip or '-'}")
    else:
        detail = f": {result.detail}" if result.detail else ""
        print(f"{result.code}{detail}", file=sys.stderr)
    if result.ok:
        return 0
    if result.code == "NOTE_CONFLICT":
        return 1
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cairnwork-attest",
        description="Offline Cairnwork attestation writer and verifier",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_keygen = subparsers.add_parser("keygen", help="Generate Ed25519 signing keypair")
    p_keygen.add_argument("--state", type=Path, required=True, help="State directory")

    p_stmt = subparsers.add_parser("statement", help="Build and optionally sign statement")
    p_stmt.add_argument("--state", type=Path, required=True, help="State directory")
    p_stmt.add_argument("--task-id", type=str, required=True, help="Task ID")
    p_stmt.add_argument("--attempt-id", type=str, required=True, help="Attempt ID")
    p_stmt.add_argument("--candidate", type=str, required=True, help="Candidate commit SHA")
    p_stmt.add_argument("--out", type=Path, required=True, help="Output file path")
    p_stmt.add_argument("--sign", action="store_true", help="Sign statement into DSSE bundle")

    p_ver = subparsers.add_parser("verify", help="Verify signed bundle offline")
    p_ver.add_argument("bundle", type=Path, help="Bundle file path")
    p_ver.add_argument("--public-key", type=Path, required=True, help="Public key PEM file path")
    p_ver.add_argument("--repo", type=Path, required=True, help="Git repository directory")
    p_ver.add_argument(
        "--candidate", type=str, required=True, help="Expected candidate commit SHA (40-hex)"
    )
    p_ver.add_argument(
        "--base",
        type=str,
        default=None,
        help="Require the statement's base commit to equal this SHA (40-hex)",
    )
    p_ver.add_argument(
        "--core-identity", type=Path, default=None, help="Path to Core identity JSON pin file"
    )
    p_ver.add_argument(
        "--json",
        action="store_true",
        help=(
            "Print verification result as a single JSON line on stdout "
            "(keyid in facts is claimed by the envelope, not proof of which key verified)"
        ),
    )

    p_pub = subparsers.add_parser("publish", help="Publish signed bundle as git note")
    p_pub.add_argument("--bundle", type=Path, required=True, help="Signed bundle file path")
    p_pub.add_argument("--repo", type=Path, required=True, help="Git repository directory")
    p_pub.add_argument("--candidate", type=str, required=True, help="Candidate commit SHA (40-hex)")
    p_pub.add_argument("--remote", type=str, default="origin", help="Git remote name")
    p_pub.add_argument(
        "--notes-ref",
        type=str,
        default="refs/notes/cairnwork",
        help="Notes ref path",
    )
    p_pub.add_argument("--max-attempts", type=int, default=5, help="Max push retry attempts")
    p_pub.add_argument("--json", action="store_true", help="JSON output")

    _add_check_run(subparsers)
    _add_on_verified(subparsers)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if args.command == "keygen":
        return _cmd_keygen(args)
    if args.command == "statement":
        return _cmd_statement(args)
    if args.command == "verify":
        return _cmd_verify(args)
    if args.command == "publish":
        return _cmd_publish(args)
    if args.command == "check-run":
        return cmd_check_run(args)
    if args.command == "on-verified":
        return cmd_on_verified(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
