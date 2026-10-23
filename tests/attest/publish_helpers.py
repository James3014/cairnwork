"""Shared fixtures and helpers for the cairnwork-attest publish tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from conftest import git
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cairnwork_attest.dsse import _sign_bytes

_NOTES_REF = "refs/notes/cairnwork"


def _make_bundle_bytes(candidate: str, key: Ed25519PrivateKey) -> bytes:
    """Create a minimal valid signed DSSE bundle for the given candidate."""
    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "predicateType": "https://cairnwork.github.io/attestation/verified-change/v0.1",
        "predicate": {},
        "subject": [
            {
                "digest": {"gitCommit": candidate},
                "name": f"git-commit:{candidate}",
            }
        ],
    }
    payload = json.dumps(statement, sort_keys=True, separators=(",", ":")).encode("utf-8")
    envelope = _sign_bytes(payload, key)
    return json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _make_bundle_bytes_alt(candidate: str, key: Ed25519PrivateKey) -> bytes:
    """Create a different valid bundle for the same candidate (different predicate)."""
    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "predicateType": "https://cairnwork.github.io/attestation/verified-change/v0.1",
        "predicate": {"extra": "data"},
        "subject": [
            {
                "digest": {"gitCommit": candidate},
                "name": f"git-commit:{candidate}",
            }
        ],
    }
    payload = json.dumps(statement, sort_keys=True, separators=(",", ":")).encode("utf-8")
    envelope = _sign_bytes(payload, key)
    return json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")


def make_publish_env(tmp_path: Path) -> tuple[Path, Path, str]:
    """Set up a repo with one commit and a bare remote."""
    src = tmp_path / "src"
    src.mkdir()
    git(src, "init")
    git(src, "config", "user.email", "test@test.com")
    git(src, "config", "user.name", "Test")
    (src / "f.txt").write_text("hello\n")
    git(src, "add", "f.txt")
    git(src, "commit", "-m", "init")
    candidate = git(src, "rev-parse", "HEAD")

    bare = tmp_path / "bare.git"
    git(tmp_path, "clone", "--bare", str(src), str(bare))
    git(src, "remote", "add", "origin", str(bare))

    return src, bare, candidate


def _write_bundle(tmp_path: Path, data: bytes, name: str = "bundle.json") -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def _clone_bare(tmp_path: Path, bare: Path, name: str = "clone2") -> Path:
    clone = tmp_path / name
    git(tmp_path, "clone", str(bare), str(clone))
    git(clone, "config", "user.email", "test@test.com")
    git(clone, "config", "user.name", "Test")
    return clone


def _read_note_from_bare(bare: Path, candidate: str) -> str:
    """Read a note for a candidate from a bare repo via a temp clone."""
    proc = subprocess.run(
        ["git", "notes", "--ref", _NOTES_REF, "show", candidate],
        cwd=bare,
        capture_output=True,
        check=False,
    )
    return proc.stdout.decode("utf-8")


def _notes_tip(repo: Path) -> str:
    return git(repo, "rev-parse", _NOTES_REF)


def _remote_tip(bare: Path) -> str | None:
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", _NOTES_REF],
        cwd=bare,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() or None
