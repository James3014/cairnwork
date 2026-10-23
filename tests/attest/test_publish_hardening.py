"""Tests for cairnwork_attest.publish: argument safety, races and failure paths."""

from __future__ import annotations

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
    _read_note_from_bare,
    _remote_tip,
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


@pytest.mark.parametrize(
    "remote",
    ["--upload-pack=touch PWNED", "-o", "origin;id", "ext::sh -c id", ""],
)
def test_unsafe_remote_rejected(
    tmp_path: Path, publish_env: tuple[Path, Path, str], key: Ed25519PrivateKey, remote: str
) -> None:
    src, bare, candidate = publish_env
    bundle_file = _write_bundle(tmp_path, _make_bundle_bytes(candidate, key))
    r = publish_bundle(bundle_file, src, candidate, remote=remote)
    assert (r.ok, r.code) == (False, "USAGE")
    assert not (src / "PWNED").exists()
    assert _remote_tip(bare) is None


@pytest.mark.parametrize(
    "notes_ref",
    ["refs/heads/main", "refs/notes/", "--ref=x", "refs/notes/a..b", "cairnwork"],
)
def test_bad_notes_ref_rejected_and_branches_untouched(
    tmp_path: Path, publish_env: tuple[Path, Path, str], key: Ed25519PrivateKey, notes_ref: str
) -> None:
    src, bare, candidate = publish_env
    heads_before = git(src, "for-each-ref", "refs/heads")
    bundle_file = _write_bundle(tmp_path, _make_bundle_bytes(candidate, key))
    r = publish_bundle(bundle_file, src, candidate, notes_ref=notes_ref)
    assert (r.ok, r.code) == (False, "USAGE")
    assert git(src, "for-each-ref", "refs/heads") == heads_before


def test_max_attempts_below_one_is_usage(
    tmp_path: Path, publish_env: tuple[Path, Path, str], key: Ed25519PrivateKey
) -> None:
    src, _bare, candidate = publish_env
    bundle_file = _write_bundle(tmp_path, _make_bundle_bytes(candidate, key))
    assert publish_bundle(bundle_file, src, candidate, max_attempts=0).code == "USAGE"


def test_same_candidate_race_ends_in_conflict(
    tmp_path: Path, publish_env: tuple[Path, Path, str], key: Ed25519PrivateKey
) -> None:
    """A competitor publishes a different bundle for our candidate between fetch and push."""
    src, bare, candidate = publish_env
    clone2 = _clone_bare(tmp_path, bare)
    theirs = _make_bundle_bytes_alt(candidate, key)
    their_blob = _git(clone2, "hash-object", "-w", "--stdin", input=theirs).stdout.strip()
    fired = {"n": 0}

    def racing_run(repo: Path, *args: str, input: bytes | None = None):  # noqa: ANN202
        result = _git(repo, *args, input=input)
        if args[:1] == ("notes",) and "add" in args and fired["n"] == 0:
            fired["n"] += 1
            _git(clone2, "notes", f"--ref={_NOTES_REF}", "add", "-C", their_blob, candidate)
            _git(clone2, "push", "origin", f"{_NOTES_REF}:{_NOTES_REF}")
        return result

    tip_after_competitor = None
    bundle_file = _write_bundle(tmp_path, _make_bundle_bytes(candidate, key))
    r = publish_bundle(bundle_file, src, candidate, run=racing_run)
    tip_after_competitor = _remote_tip(bare)
    assert (r.ok, r.code, r.attempts) == (False, "NOTE_CONFLICT", 2)
    assert _read_note_from_bare(bare, candidate).encode("utf-8") == theirs
    assert _remote_tip(bare) == tip_after_competitor


def test_push_rejected_by_hook_is_push_failed(
    tmp_path: Path, publish_env: tuple[Path, Path, str], key: Ed25519PrivateKey
) -> None:
    src, bare, candidate = publish_env
    hook = bare / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho denied by policy >&2\nexit 1\n")
    hook.chmod(0o755)
    bundle_file = _write_bundle(tmp_path, _make_bundle_bytes(candidate, key))
    r = publish_bundle(bundle_file, src, candidate)
    assert (r.ok, r.code) == (False, "PUSH_FAILED")
    assert r.detail and "denied by policy" in r.detail
    assert _remote_tip(bare) is None


def test_retries_exhausted(
    tmp_path: Path, publish_env: tuple[Path, Path, str], key: Ed25519PrivateKey
) -> None:
    src, bare, candidate = publish_env
    clone2 = _clone_bare(tmp_path, bare)
    counter = {"n": 0}

    def always_beaten(repo: Path, *args: str, input: bytes | None = None):  # noqa: ANN202
        if args[:1] == ("push",) and repo == src:
            counter["n"] += 1
            (clone2 / f"c{counter['n']}.txt").write_text("x\n")
            git(clone2, "add", ".")
            git(clone2, "commit", "-m", f"c{counter['n']}")
            sha = git(clone2, "rev-parse", "HEAD")
            _git(clone2, "fetch", "origin", f"+{_NOTES_REF}:{_NOTES_REF}")
            _git(clone2, "notes", f"--ref={_NOTES_REF}", "add", "-m", "other", sha)
            _git(clone2, "push", "origin", "HEAD", f"{_NOTES_REF}:{_NOTES_REF}")
        return _git(repo, *args, input=input)

    bundle_file = _write_bundle(tmp_path, _make_bundle_bytes(candidate, key))
    r = publish_bundle(bundle_file, src, candidate, max_attempts=3, run=always_beaten)
    assert (r.ok, r.code, r.attempts) == (False, "PUSH_RETRIES_EXHAUSTED", 3)
    assert counter["n"] == 3


def test_cli_exit_codes(
    tmp_path: Path, publish_env: tuple[Path, Path, str], key: Ed25519PrivateKey
) -> None:
    src, _bare, candidate = publish_env
    ours = _write_bundle(tmp_path, _make_bundle_bytes(candidate, key))
    other = _write_bundle(tmp_path, _make_bundle_bytes_alt(candidate, key), name="b2.json")
    base = ["publish", "--repo", str(src), "--candidate", candidate]
    assert main([*base, "--bundle", str(ours)]) == 0
    assert main([*base, "--bundle", str(ours)]) == 0
    assert main([*base, "--bundle", str(other)]) == 1
    assert main([*base, "--bundle", str(ours), "--remote", "--upload-pack=x"]) == 2


def test_noop_and_conflict_seen_from_fresh_clone(
    tmp_path: Path, publish_env: tuple[Path, Path, str], key: Ed25519PrivateKey
) -> None:
    """A second publisher with no local notes ref must still see the remote note."""
    src, bare, candidate = publish_env
    ours = _make_bundle_bytes(candidate, key)
    first = publish_bundle(_write_bundle(tmp_path, ours), src, candidate)
    assert first.code == "PUBLISHED"
    tip = _remote_tip(bare)

    fresh = _clone_bare(tmp_path, bare, name="fresh2")
    again = publish_bundle(_write_bundle(tmp_path, ours, name="same.json"), fresh, candidate)
    assert (again.code, again.notes_tip) == ("NOOP", tip)

    fresh3 = _clone_bare(tmp_path, bare, name="fresh3")
    other = _write_bundle(tmp_path, _make_bundle_bytes_alt(candidate, key), name="o.json")
    clash = publish_bundle(other, fresh3, candidate)
    assert (clash.ok, clash.code) == (False, "NOTE_CONFLICT")
    assert _remote_tip(bare) == tip
    assert _read_note_from_bare(bare, candidate).encode("utf-8") == ours


def test_failed_push_leaves_no_local_note(
    tmp_path: Path, publish_env: tuple[Path, Path, str], key: Ed25519PrivateKey
) -> None:
    src, bare, candidate = publish_env
    hook = bare / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    bundle_file = _write_bundle(tmp_path, _make_bundle_bytes(candidate, key))
    assert publish_bundle(bundle_file, src, candidate).code == "PUSH_FAILED"
    local = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", _NOTES_REF], cwd=src, check=False
    )
    assert local.returncode != 0


def test_remote_lock_failure_is_retried(
    tmp_path: Path, publish_env: tuple[Path, Path, str], key: Ed25519PrivateKey
) -> None:
    src, bare, candidate = publish_env
    calls = {"push": 0}

    def lock_once(repo: Path, *args: str, input: bytes | None = None):  # noqa: ANN202
        if args[:1] == ("push",) and calls["push"] == 0:
            calls["push"] += 1
            return subprocess.CompletedProcess(
                ["git", *args],
                1,
                "",
                " ! [remote rejected] refs/notes/cairnwork -> refs/notes/cairnwork "
                "(failed to update ref)\nerror: cannot lock ref 'refs/notes/cairnwork'\n",
            )
        return _git(repo, *args, input=input)

    bundle_file = _write_bundle(tmp_path, _make_bundle_bytes(candidate, key))
    r = publish_bundle(bundle_file, src, candidate, run=lock_once)
    assert (r.ok, r.code, r.attempts) == (True, "PUBLISHED", 2)
