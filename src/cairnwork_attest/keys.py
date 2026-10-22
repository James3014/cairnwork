"""Ed25519 signing keys for attestations: generate into the state dir, load, print.

The private key lives only in the state directory (mode 0600, never overwritten).
Nothing here falls back to a default key: a missing, loose or malformed key file is
an error, so signing fails closed.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import os
import stat
import sys
from pathlib import Path

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from cairnwork_attest.record import AttestError

PRIVATE_KEY_FILENAME = "attest_ed25519.pem"
_LOOSE_BITS = stat.S_IRWXG | stat.S_IRWXO


class KeyFileError(AttestError):
    code: str = "KEY_FILE_ERROR"


def private_key_path(state_dir: Path) -> Path:
    return state_dir / PRIVATE_KEY_FILENAME


def keyid(public_key: Ed25519PublicKey) -> str:
    """SHA-256 (hex) of the raw 32-byte public key: the DSSE keyid hint."""
    raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()


def public_key_pem(public_key: Ed25519PublicKey) -> str:
    return public_key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("ascii")


def load_public_key_pem(pem: str | bytes) -> Ed25519PublicKey:
    data = pem.encode("ascii") if isinstance(pem, str) else pem
    try:
        key = serialization.load_pem_public_key(data)
    except (ValueError, UnsupportedAlgorithm) as exc:
        raise KeyFileError(f"public key is not valid PEM: {exc}", code="KEY_MALFORMED") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise KeyFileError("public key is not Ed25519", code="KEY_WRONG_TYPE")
    return key


def _safe_unlink(path: Path, expected_dev: int, expected_ino: int) -> None:
    try:
        st = os.lstat(path)
        if st.st_dev == expected_dev and st.st_ino == expected_ino:
            path.unlink()
    except OSError:
        return


def _fsync_dir(dir_path: Path) -> None:
    dir_fd = os.open(dir_path, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def generate_keypair(state_dir: Path) -> Ed25519PublicKey:
    """Create a new key pair; write the private key (0600) and return the public key.

    Refuses to overwrite an existing key file (KEY_EXISTS) or to follow a symlink.
    """
    if not state_dir.is_dir():
        raise KeyFileError(f"state dir not found: {state_dir}", code="STATE_DIR_NOT_FOUND")
    private_key = Ed25519PrivateKey.generate()
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path = private_key_path(state_dir)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise KeyFileError(f"key file already exists: {path}", code="KEY_EXISTS") from exc
    except OSError as exc:
        raise KeyFileError(f"cannot create key file: {exc}", code="KEY_UNWRITABLE") from exc

    try:
        created_stat = os.fstat(fd)
        created_dev = created_stat.st_dev
        created_ino = created_stat.st_ino

        os.fchmod(fd, 0o600)  # independent of umask and of what O_CREAT honoured
        offset = 0
        while offset < len(pem):
            written = os.write(fd, pem[offset:])
            if written <= 0:
                raise OSError("os.write returned 0 bytes")
            offset += written
        os.fsync(fd)
    except OSError as exc:
        os.close(fd)
        _safe_unlink(path, created_dev, created_ino)
        raise KeyFileError(f"cannot write key file: {exc}", code="KEY_UNWRITABLE") from exc

    os.close(fd)

    try:
        _fsync_dir(state_dir)
    except OSError as exc:
        _safe_unlink(path, created_dev, created_ino)
        raise KeyFileError(f"cannot sync directory: {exc}", code="KEY_UNWRITABLE") from exc

    return private_key.public_key()


def load_private_key(state_dir: Path) -> Ed25519PrivateKey:
    """Load the signing key; reject symlinks, non-regular files and group/other access."""
    path = private_key_path(state_dir)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        if os.path.islink(path):
            raise KeyFileError(
                f"key file is not a regular file (symlink): {path}", code="KEY_NOT_REGULAR"
            ) from exc
        raise KeyFileError(f"no signing key at {path}", code="KEY_NOT_FOUND") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP or os.path.islink(path):
            raise KeyFileError(
                f"key file is not a regular file (symlink): {path}", code="KEY_NOT_REGULAR"
            ) from exc
        raise KeyFileError(f"cannot open key file: {exc}", code="KEY_UNREADABLE") from exc

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise KeyFileError(f"key file is not a regular file: {path}", code="KEY_NOT_REGULAR")
        if st.st_mode & _LOOSE_BITS:
            raise KeyFileError(
                f"key file mode {stat.S_IMODE(st.st_mode):o} is not 0600: {path}",
                code="KEY_PERMISSIONS_LOOSE",
            )
        if st.st_uid != os.getuid():
            raise KeyFileError(
                f"key file owner {st.st_uid} does not match user {os.getuid()}: {path}",
                code="KEY_WRONG_OWNER",
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
    finally:
        os.close(fd)

    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
        raise KeyFileError(f"key file is not a valid PEM key: {exc}", code="KEY_MALFORMED") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise KeyFileError("key file does not hold an Ed25519 key", code="KEY_WRONG_TYPE")
    return key


def main(argv: list[str] | None = None) -> int:
    """`python -m cairnwork_attest.keys {generate,public} --state DIR`."""
    parser = argparse.ArgumentParser(prog="python -m cairnwork_attest.keys")
    parser.add_argument("command", choices=("generate", "public"))
    parser.add_argument("--state", type=Path, required=True, help="Cairnwork state directory")
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            public_key = generate_keypair(args.state)
        else:
            public_key = load_private_key(args.state).public_key()
    except KeyFileError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"keyid: {keyid(public_key)}")
    print(public_key_pem(public_key), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
