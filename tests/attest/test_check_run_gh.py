"""Tests for the --gh transport of cairnwork_attest.checkrun (fake gh, no network)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cairnwork_attest.checkrun import post_check_run
from cairnwork_attest.checkrun_gh import _parse_include
from cairnwork_attest.cli import main
from tests.attest.test_check_run import CAND, SLUG, _bundle, _pem

_FAKE_GH = """#!/bin/sh
printf '%s\\n' "$@" > "{log}/argv"
cat > "{log}/stdin"
env > "{log}/env"
printf '{head}\\r\\nContent-Type: application/json\\r\\n\\r\\n{body}'
exit {code}
"""


def _gh(tmp_path: Path, head="HTTP/2.0 201 Created", body='{"id": 9}', code=0) -> Path:
    path = tmp_path / "fake-gh"
    script = _FAKE_GH.format(log=tmp_path, head=head, body=body, code=code)
    path.write_text(script)
    path.chmod(0o755)
    return path


def test_gh_mode_posts_without_any_token(tmp_path, monkeypatch):
    monkeypatch.delenv("CAIRNWORK_GITHUB_TOKEN", raising=False)
    key = Ed25519PrivateKey.generate()
    gh = _gh(tmp_path)
    result = post_check_run(
        _bundle(tmp_path, key), SLUG, public_key=_pem(tmp_path, key), token=None, gh=gh
    )
    assert (result.ok, result.code, result.check_run_id) == (True, "CREATED", 9)
    argv = (tmp_path / "argv").read_text().splitlines()
    assert argv[:5] == ["api", "--method", "POST", "--include", f"repos/{SLUG}/check-runs"]
    assert argv[-2:] == ["--input", "-"]
    assert not any("Authorization" in a for a in argv)
    sent = json.loads((tmp_path / "stdin").read_text())
    assert (sent["head_sha"], sent["conclusion"]) == (CAND, "neutral")


def test_gh_mode_ignores_env_token(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRNWORK_GITHUB_TOKEN", "ghs_SHOULDNOTLEAK")
    key = Ed25519PrivateKey.generate()
    result = post_check_run(_bundle(tmp_path, key), SLUG, token="bad token!", gh=_gh(tmp_path))
    assert result.ok
    assert "ghs_SHOULDNOTLEAK" not in (tmp_path / "argv").read_text()


def test_gh_http_error_is_reported(tmp_path):
    key = Ed25519PrivateKey.generate()
    gh = _gh(tmp_path, head="HTTP/2.0 403 Forbidden", body='{"message": "no"}', code=1)
    result = post_check_run(_bundle(tmp_path, key), SLUG, token=None, gh=gh)
    assert (result.ok, result.code, result.detail) == (False, "HTTP_ERROR", "HTTP 403: no")


def test_gh_without_output_is_network_error(tmp_path):
    key = Ed25519PrivateKey.generate()
    gh = tmp_path / "silent-gh"
    gh.write_text("#!/bin/sh\necho 'auth failed' >&2\nexit 1\n")
    gh.chmod(0o755)
    result = post_check_run(_bundle(tmp_path, key), SLUG, token=None, gh=gh)
    assert (result.ok, result.code) == (False, "NETWORK_ERROR")


@pytest.mark.parametrize("make", ["relative", "missing", "not-exec"])
def test_bad_gh_path_is_usage(tmp_path, make):
    key = Ed25519PrivateKey.generate()
    gh = {"relative": Path("gh"), "missing": tmp_path / "nope", "not-exec": tmp_path / "plain"}[
        make
    ]
    if make == "not-exec":
        gh.write_text("#!/bin/sh\n")
    result = post_check_run(_bundle(tmp_path, key), SLUG, token=None, gh=gh)
    assert (result.ok, result.code) == (False, "USAGE")
    assert not (tmp_path / "argv").exists()


@pytest.mark.parametrize(
    "out",
    [b"", b"HTTP/2.0 201\r\n", b"garbage\r\n\r\n{}", b"HTTP/2.0 abc\r\n\r\n{}"],
)
def test_parse_include_rejects_bad_output(out):
    with pytest.raises(OSError):
        _parse_include(out)


def test_parse_include_lf_only():
    assert _parse_include(b'HTTP/1.1 201 Created\nA: b\n\n{"id": 1}') == (201, b'{"id": 1}')


def test_cli_check_run_gh(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("CAIRNWORK_GITHUB_TOKEN", raising=False)
    key = Ed25519PrivateKey.generate()
    gh = _gh(tmp_path)
    rc = main(
        [
            "check-run",
            "--bundle",
            str(_bundle(tmp_path, key)),
            "--repo-slug",
            SLUG,
            "--gh",
            str(gh),
            "--json",
        ]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["code"] == "CREATED"
    assert os.path.exists(tmp_path / "stdin")


def test_on_verified_gh_posts_without_token(tmp_path, repo, monkeypatch, capsys):
    from tests.attest.test_on_verified import Env, _codes, _run

    monkeypatch.delenv("CAIRNWORK_GITHUB_TOKEN", raising=False)
    env = Env(tmp_path, repo)
    capsys.readouterr()
    gh = _gh(tmp_path)
    rc, _, obj = _run(capsys, env.argv("--gh", str(gh)))
    assert rc == 0
    assert _codes(obj) == [("statement", "OK"), ("publish", "PUBLISHED"), ("check_run", "CREATED")]
    assert json.loads((tmp_path / "stdin").read_text())["head_sha"] == env.candidate


def test_on_verified_bad_gh_publishes_then_usage(tmp_path, repo, capsys):
    from tests.attest.test_on_verified import Env, _codes, _run

    env = Env(tmp_path, repo)
    capsys.readouterr()
    rc, _, obj = _run(capsys, env.argv("--gh", "relative-gh"))
    assert rc == 2
    assert _codes(obj) == [("statement", "OK"), ("publish", "PUBLISHED"), ("check_run", "USAGE")]
