"""Offline DSSE bundle writing and reading with strict JSON validation."""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cairnwork_attest.record import AttestError, parse_strict_json
from cairnwork_protocol import canonical_json

MAX_BUNDLE_BYTES: int = 4 * 1024 * 1024  # 4 MiB


class BundleError(AttestError):
    code: str = "BUNDLE_ERROR"


def write_bundle(path: Path, envelope: Mapping[str, Any]) -> None:
    """Write the DSSE envelope as canonical JSON with O_EXCL, mode 0644."""
    if not isinstance(envelope, Mapping):
        raise BundleError("envelope must be a Mapping", code="BUNDLE_MALFORMED")
    p = Path(path)
    try:
        data = canonical_json(envelope).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise BundleError(f"cannot encode envelope: {exc}", code="BUNDLE_MALFORMED") from exc

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(p, flags, 0o644)
    except FileExistsError as exc:
        raise BundleError(f"bundle already exists: {p}", code="BUNDLE_EXISTS") from exc
    except OSError as exc:
        raise BundleError(f"cannot create bundle file: {exc}", code="BUNDLE_UNWRITABLE") from exc

    try:
        os.fchmod(fd, 0o644)
        os.write(fd, data)
        os.fsync(fd)
    except OSError as exc:
        os.close(fd)
        p.unlink(missing_ok=True)
        raise BundleError(f"cannot write bundle: {exc}", code="BUNDLE_UNWRITABLE") from exc
    os.close(fd)


def read_bundle(path: Path) -> dict[str, Any]:
    """Read a DSSE envelope JSON file under 4 MiB with strict JSON validation."""
    p = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(p, flags)
    except FileNotFoundError as exc:
        if os.path.islink(p):
            raise BundleError(
                f"bundle is not a regular file (symlink): {p}", code="BUNDLE_NOT_REGULAR"
            ) from exc
        raise BundleError(f"bundle not found: {p}", code="BUNDLE_NOT_FOUND") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP or os.path.islink(p):
            raise BundleError(
                f"bundle is not a regular file (symlink): {p}", code="BUNDLE_NOT_REGULAR"
            ) from exc
        raise BundleError(f"cannot open bundle: {exc}", code="BUNDLE_UNREADABLE") from exc

    try:
        try:
            st = os.fstat(fd)
        except OSError as exc:
            raise BundleError(f"cannot stat bundle: {exc}", code="BUNDLE_UNREADABLE") from exc

        if not stat.S_ISREG(st.st_mode):
            raise BundleError(f"bundle is not a regular file: {p}", code="BUNDLE_NOT_REGULAR")

        if st.st_size > MAX_BUNDLE_BYTES:
            raise BundleError(
                f"bundle size {st.st_size} exceeds {MAX_BUNDLE_BYTES} bytes",
                code="BUNDLE_TOO_LARGE",
            )

        chunks: list[bytes] = []
        total_bytes = 0
        limit = MAX_BUNDLE_BYTES + 1
        while total_bytes < limit:
            to_read = min(65536, limit - total_bytes)
            try:
                chunk = os.read(fd, to_read)
            except OSError as exc:
                raise BundleError(f"cannot read bundle: {exc}", code="BUNDLE_UNREADABLE") from exc
            if not chunk:
                break
            chunks.append(chunk)
            total_bytes += len(chunk)

        if total_bytes > MAX_BUNDLE_BYTES:
            raise BundleError(
                f"bundle size exceeds {MAX_BUNDLE_BYTES} bytes",
                code="BUNDLE_TOO_LARGE",
            )
        raw_bytes = b"".join(chunks)
    finally:
        os.close(fd)

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BundleError(f"bundle is not UTF-8: {exc}", code="BUNDLE_MALFORMED") from exc

    parsed = parse_strict_json(
        text,
        error_cls=BundleError,
        malformed_code="BUNDLE_MALFORMED",
        constant_code="BUNDLE_MALFORMED",
    )
    if not isinstance(parsed, dict):
        raise BundleError("bundle must decode to a JSON object", code="BUNDLE_MALFORMED")
    return parsed
