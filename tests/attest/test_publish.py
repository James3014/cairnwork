"""Tests for cairnwork_attest.publish: core behaviour."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from conftest import git
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from publish_helpers import (
    _NOTES_REF,
    _clone_bare,
    _make_bundle_bytes,
    _make_bundle_bytes_alt,
    _notes_tip,
    _read_note_from_bare,
    _write_bundle,
    make_publish_env,
)

from cairnwork_attest.cli import main
from cairnwork_attest.publish import _git, publish_bundle


@pytest.fixture
def key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def publish_env(tmp_path: Path) -> tuple[Path, Path, str]:
    return make_publish_env(tmp_path)


def test_first_publish(
    tmp_path: Path, publish_env: tuple[Path, Path, str], key: Ed25519PrivateKey
) -> None:
    src, bare, candidate = publish_env
    bundle_bytes = _make_bundle_bytes(candidate, key)
    bundle_file = _write_bundle(tmp_path, bundle_bytes)

    result = publish_bundle(bundle_file, src, candidate)

    assert result.ok is True
    assert result.code == "PUBLISHED"
    assert result.candidate == candidate
    assert result.attempts == 1
    assert result.blob is not None
    assert result.notes_tip is not None

    # Verify from bare repo
    note_content = _read_note_from_bare(bare, candidate)
    assert note_content.encode("utf-8") == bundle_bytes


def test_noop_same_bundle(
    tmp_path: Path, publish_env: tuple[Path, Path, str], key: Ed25519PrivateKey
) -> None:
    src, bare, candidate = publish_env
    bundle_bytes = _make_bundle_bytes(candidate, key)
    bundle_file = _write_bundle(tmp_path, bundle_bytes)

    r1 = publish_bundle(bundle_file, src, candidate)
    assert r1.code == "PUBLISHED"
    tip_after_first = _notes_tip(bare)

    r2 = publish_bundle(bundle_file, src, candidate)
    assert r2.ok is True
    assert r2.code == "NOOP"
    assert _notes_tip(bare) == tip_after_first


def test_conflict_different_bundle(
    tmp_path: Path, publish_env: tuple[Path, Path, str], key: Ed25519PrivateKey
) -> None:
    src, bare, candidate = publish_env
    bundle_file = _write_bundle(tmp_path, _make_bundle_bytes(candidate, key))
    r1 = publish_bundle(bundle_file, src, candidate)
    assert r1.code == "PUBLISHED"
    tip_before = _notes_tip(bare)

    alt_bytes = _make_bundle_bytes_alt(candidate, key)
    alt_file = _write_bundle(tmp_path, alt_bytes, "alt.json")
    r2 = publish_bundle(alt_file, src, candidate)

    assert r2.ok is False
    assert r2.code == "NOTE_CONFLICT"
    assert _notes_tip(bare) == tip_before


def test_coexist_with_other_notes(
    tmp_path: Path, publish_env: tuple[Path, Path, str], key: Ed25519PrivateKey
) -> None:
    src, bare, candidate = publish_env

    # Create a second commit in a clone and publish a note for it
    clone2 = _clone_bare(tmp_path, bare)
    (clone2 / "g.txt").write_text("world\n")
    git(clone2, "add", "g.txt")
    git(clone2, "commit", "-m", "second")
    other_sha = git(clone2, "rev-parse", "HEAD")
    git(clone2, "push", "origin", "HEAD")

    other_bundle = _make_bundle_bytes(other_sha, key)
    other_file = _write_bundle(tmp_path, other_bundle, "other.json")
    r_other = publish_bundle(other_file, clone2, other_sha, remote="origin")
    assert r_other.code == "PUBLISHED"

    # Now publish ours from src
    bundle_bytes = _make_bundle_bytes(candidate, key)
    bundle_file = _write_bundle(tmp_path, bundle_bytes)
    r_ours = publish_bundle(bundle_file, src, candidate)
    assert r_ours.code == "PUBLISHED"

    # Both notes readable
    assert _read_note_from_bare(bare, candidate).encode("utf-8") == bundle_bytes
    assert _read_note_from_bare(bare, other_sha).encode("utf-8") == other_bundle


def test_concurrent_push_retry(
    tmp_path: Path, publish_env: tuple[Path, Path, str], key: Ed25519PrivateKey
) -> None:
    src, bare, candidate = publish_env

    # Set up a second clone with a different commit
    clone2 = _clone_bare(tmp_path, bare)
    (clone2 / "g.txt").write_text("concurrent\n")
    git(clone2, "add", "g.txt")
    git(clone2, "commit", "-m", "concurrent")
    other_sha = git(clone2, "rev-parse", "HEAD")
    git(clone2, "push", "origin", "HEAD")

    other_bundle = _make_bundle_bytes(other_sha, key)
    other_blob_proc = _git(clone2, "hash-object", "-w", "--stdin", input=other_bundle)
    other_blob = other_blob_proc.stdout.strip()

    injected = {"count": 0}

    def intercepting_run(
        repo: Path, *args: str, input: bytes | None = None
    ) -> subprocess.CompletedProcess[str]:
        result = _git(repo, *args, input=input)
        # After our first "notes add", push a competing note from clone2
        if len(args) >= 2 and args[0] == "notes" and "add" in args and injected["count"] == 0:
            injected["count"] += 1
            _git(clone2, "fetch", "origin", f"+{_NOTES_REF}:{_NOTES_REF}")
            _git(clone2, "notes", f"--ref={_NOTES_REF}", "add", "-C", other_blob, other_sha)
            _git(clone2, "push", "origin", f"{_NOTES_REF}:{_NOTES_REF}")
        return result

    bundle_bytes = _make_bundle_bytes(candidate, key)
    bundle_file = _write_bundle(tmp_path, bundle_bytes)
    r = publish_bundle(bundle_file, src, candidate, run=intercepting_run)

    assert r.ok is True
    assert r.code == "PUBLISHED"
    assert r.attempts == 2

    # Both notes present
    assert _read_note_from_bare(bare, candidate).encode("utf-8") == bundle_bytes
    assert _read_note_from_bare(bare, other_sha).encode("utf-8") == other_bundle


def test_candidate_not_in_repo(
    tmp_path: Path, publish_env: tuple[Path, Path, str], key: Ed25519PrivateKey
) -> None:
    src, _bare, _candidate = publish_env
    fake_sha = "a" * 40
    bundle_file = _write_bundle(tmp_path, _make_bundle_bytes(fake_sha, key))

    r = publish_bundle(bundle_file, src, fake_sha)

    assert r.ok is False
    assert r.code == "CANDIDATE_NOT_FOUND"


def test_candidate_mismatch(
    tmp_path: Path, publish_env: tuple[Path, Path, str], key: Ed25519PrivateKey
) -> None:
    src, _bare, candidate = publish_env
    wrong_sha = "b" * 40
    # Bundle built for wrong_sha, but we pass candidate as the expected
    bundle_file = _write_bundle(tmp_path, _make_bundle_bytes(wrong_sha, key))

    r = publish_bundle(bundle_file, src, candidate)

    assert r.ok is False
    assert r.code == "CANDIDATE_MISMATCH"


def test_malformed_candidate(tmp_path: Path, publish_env: tuple[Path, Path, str]) -> None:
    src, _bare, _candidate = publish_env
    bundle_file = tmp_path / "dummy.json"
    bundle_file.write_text("{}", encoding="utf-8")

    r = publish_bundle(bundle_file, src, "not-a-sha")

    assert r.ok is False
    assert r.code == "USAGE"


def test_malformed_bundle(tmp_path: Path, publish_env: tuple[Path, Path, str]) -> None:
    src, _bare, candidate = publish_env
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("not json at all {{{", encoding="utf-8")

    r = publish_bundle(bad_file, src, candidate)

    assert r.ok is False
    assert r.code == "BUNDLE_MALFORMED"


def test_json_output_published(
    tmp_path: Path,
    publish_env: tuple[Path, Path, str],
    key: Ed25519PrivateKey,
    capsys: pytest.CaptureFixture[str],
) -> None:
    src, _bare, candidate = publish_env
    bundle_file = _write_bundle(tmp_path, _make_bundle_bytes(candidate, key))

    rc = main(
        [
            "publish",
            "--bundle",
            str(bundle_file),
            "--repo",
            str(src),
            "--candidate",
            candidate,
            "--json",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["ok"] is True
    assert data["code"] == "PUBLISHED"
    assert data["candidate"] == candidate
    assert list(data.keys()) == sorted(data.keys())


def test_json_output_conflict(
    tmp_path: Path,
    publish_env: tuple[Path, Path, str],
    key: Ed25519PrivateKey,
    capsys: pytest.CaptureFixture[str],
) -> None:
    src, _bare, candidate = publish_env
    bundle_file = _write_bundle(tmp_path, _make_bundle_bytes(candidate, key))
    publish_bundle(bundle_file, src, candidate)

    alt_file = _write_bundle(tmp_path, _make_bundle_bytes_alt(candidate, key), "alt2.json")
    capsys.readouterr()  # clear
    rc = main(
        [
            "publish",
            "--bundle",
            str(alt_file),
            "--repo",
            str(src),
            "--candidate",
            candidate,
            "--json",
        ]
    )
    assert rc == 1
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["ok"] is False
    assert data["code"] == "NOTE_CONFLICT"
    assert list(data.keys()) == sorted(data.keys())


def test_fetch_failed_unreachable(tmp_path: Path, key: Ed25519PrivateKey) -> None:
    src = tmp_path / "repo"
    src.mkdir()
    git(src, "init")
    git(src, "config", "user.email", "test@test.com")
    git(src, "config", "user.name", "Test")
    (src / "f.txt").write_text("x\n")
    git(src, "add", "f.txt")
    git(src, "commit", "-m", "init")
    candidate = git(src, "rev-parse", "HEAD")

    # Point remote at a non-existent path
    git(src, "remote", "add", "origin", str(tmp_path / "does-not-exist"))

    bundle_file = _write_bundle(tmp_path, _make_bundle_bytes(candidate, key))
    r = publish_bundle(bundle_file, src, candidate)

    assert r.ok is False
    assert r.code == "FETCH_FAILED"
    assert r.detail is not None and len(r.detail) > 0


def test_note_bytes_exact_via_verify_action_path(
    tmp_path: Path, publish_env: tuple[Path, Path, str], key: Ed25519PrivateKey
) -> None:
    """A fresh clone reading the note the way verify-action does gets the file's exact bytes."""
    src, bare, candidate = publish_env
    # Trailing whitespace and no final newline: `git notes add -F` would normalise these.
    bundle_bytes = _make_bundle_bytes(candidate, key).rstrip(b"\n") + b"  \n\n  "
    bundle_file = _write_bundle(tmp_path, bundle_bytes)
    assert publish_bundle(bundle_file, src, candidate).code == "PUBLISHED"

    fresh = tmp_path / "fresh"
    subprocess.run(["git", "clone", "-q", str(bare), str(fresh)], check=True)
    subprocess.run(
        ["git", "fetch", "origin", f"+{_NOTES_REF}:{_NOTES_REF}"],
        cwd=fresh,
        check=True,
        capture_output=True,
    )
    shown = subprocess.run(
        ["git", "notes", "--ref", _NOTES_REF, "show", candidate],
        cwd=fresh,
        check=True,
        capture_output=True,
    ).stdout
    assert shown == bundle_bytes
