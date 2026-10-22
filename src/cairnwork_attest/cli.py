"""Offline bundle writer and verifier CLI for Cairnwork attestations."""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cairnwork_attest.bundle import BundleError, write_bundle
from cairnwork_attest.dsse import sign_statement
from cairnwork_attest.keys import (
    KeyFileError,
    generate_keypair,
    keyid,
    load_private_key,
    public_key_pem,
)
from cairnwork_attest.record import AttestError, parse_strict_json, verify_record
from cairnwork_attest.statement import Producer, build_statement
from cairnwork_attest.verify import verify_bundle
from cairnwork_protocol import canonical_json


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
        try:
            version = importlib.metadata.version("cairnwork")
        except importlib.metadata.PackageNotFoundError:
            version = "0.0.1.dev0"
        producer = Producer(name="cairnwork", version=version, git_commit="")
        timestamp = datetime.now(UTC).isoformat()

        out_path = Path(args.out)
        if args.sign:
            private_key = load_private_key(state_dir)
            envelope = sign_statement(
                verified,
                producer=producer,
                timestamp=timestamp,
                private_key=private_key,
            )
            write_bundle(out_path, envelope)
        else:
            stmt = build_statement(verified, producer=producer, timestamp=timestamp)
            _write_unsigned_statement(out_path, stmt)
    except AttestError as exc:
        print(exc.code, file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    repo = Path(args.repo)
    if args.candidate is not None:
        candidate = args.candidate
    else:
        try:
            res = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
                shell=False,
            )
            candidate = res.stdout.strip()
        except (subprocess.SubprocessError, OSError) as exc:
            print(f"ERROR: cannot resolve HEAD from {repo}: {exc}", file=sys.stderr)
            return 2

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
                print("ERROR: core-identity must decode to a JSON object", file=sys.stderr)
                return 2
        except (BundleError, OSError, UnicodeDecodeError) as exc:
            print(f"ERROR: cannot read core-identity: {exc}", file=sys.stderr)
            return 2

    try:
        result = verify_bundle(
            Path(args.bundle),
            public_key_pem=Path(args.public_key),
            repo=repo,
            expected_candidate=candidate,
            core_identity_pin=core_id_pin,
        )
    except BundleError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 1

    if result.ok and result.statement is not None:
        cand = result.statement["subject"][0]["digest"]["gitCommit"]
        print(f"OK {cand}")
        return 0

    detail = f": {result.detail}" if result.detail else ""
    print(f"{result.code}{detail}")
    return 1


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
        "--candidate", type=str, default=None, help="Expected candidate commit SHA (40-hex)"
    )
    p_ver.add_argument(
        "--core-identity", type=Path, default=None, help="Path to Core identity JSON pin file"
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "keygen":
        return _cmd_keygen(args)
    if args.command == "statement":
        return _cmd_statement(args)
    if args.command == "verify":
        return _cmd_verify(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
