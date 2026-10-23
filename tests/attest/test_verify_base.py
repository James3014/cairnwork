"""Tests for the optional verify --base pin (BASE_MISMATCH)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import git

from cairnwork_attest.cli import main
from cairnwork_attest.keys import generate_keypair, public_key_pem
from cairnwork_attest.verify import verify_bundle
from tests.attest.test_verify import setup_verified_bundle_fixture


def _args(bundle: Path, pub: Path, repo: Path, candidate: str, *extra: str) -> list[str]:
    return [
        "verify",
        str(bundle),
        "--public-key",
        str(pub),
        "--repo",
        str(repo),
        "--candidate",
        candidate,
        *extra,
    ]


def test_matching_base_passes(tmp_path: Path, repo: Path) -> None:
    bundle, pub, base, cand = setup_verified_bundle_fixture(tmp_path / "s", repo)
    res = verify_bundle(
        bundle, public_key_pem=pub, repo=repo, expected_candidate=cand, expected_base=base
    )
    assert res.ok is True
    assert main(_args(bundle, pub, repo, cand, "--base", base)) == 0


def test_different_base_fails_with_base_mismatch(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle, pub, base, cand = setup_verified_bundle_fixture(tmp_path / "s", repo)
    other = "1" * 40
    assert other != base
    res = verify_bundle(
        bundle, public_key_pem=pub, repo=repo, expected_candidate=cand, expected_base=other
    )
    assert res.ok is False
    assert res.code == "BASE_MISMATCH"
    capsys.readouterr()
    rc = main(_args(bundle, pub, repo, cand, "--base", other, "--json"))
    assert rc == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert data["code"] == "BASE_MISMATCH"
    assert data["base"] == base


def test_base_mismatch_not_reached_without_valid_signature(tmp_path: Path, repo: Path) -> None:
    bundle, pub, _, cand = setup_verified_bundle_fixture(tmp_path / "s", repo)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other_pub = other_dir / "public.pem"
    other_pub.write_text(public_key_pem(generate_keypair(other_dir)), encoding="ascii")
    res = verify_bundle(
        bundle,
        public_key_pem=other_pub,
        repo=repo,
        expected_candidate=cand,
        expected_base="1" * 40,
    )
    assert res.code == "SIGNATURE_INVALID"


@pytest.mark.parametrize("bad", ["abc", "G" * 40, "A" * 40, "1" * 41, ""])
def test_malformed_base_is_usage_error(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str], bad: str
) -> None:
    bundle, pub, _, cand = setup_verified_bundle_fixture(tmp_path / "s", repo)
    rc = main(_args(bundle, pub, repo, cand, "--base", bad, "--json"))
    assert rc == 2
    captured = capsys.readouterr()
    assert "ERROR: malformed --base" in captured.err
    data = json.loads(captured.out)
    assert data["code"] == "USAGE"
    assert data["ok"] is False


def test_without_base_unchanged(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle, pub, base, cand = setup_verified_bundle_fixture(tmp_path / "s", repo)
    assert git(repo, "rev-parse", base) == base
    assert main(_args(bundle, pub, repo, cand)) == 0
    assert capsys.readouterr().out == f"OK {cand}\n"
    assert main(_args(bundle, pub, repo, cand, "--json")) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["code"] == "OK"
    assert data["base"] == base
