"""Hardening tests for cairnwork_attest.checkrun (review of #212; no network)."""

from __future__ import annotations

import base64
import http.client
import json
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cairnwork_attest import checkrun
from cairnwork_attest.cli import main
from cairnwork_attest.verify import verify_bundle
from tests.attest.test_check_run import CAND, TOKEN, FakeHttp, _bundle, _pem, _post


def _summary(http: FakeHttp) -> str:
    return json.loads(http.calls[0][3])["output"]["summary"]


def test_deeply_nested_payload_is_statement_shape(tmp_path: Path) -> None:
    env = json.loads(_bundle(tmp_path, Ed25519PrivateKey.generate()).read_text())
    env["payload"] = base64.b64encode(b"[" * 200_000).decode()
    path = tmp_path / "deep.json"
    path.write_text(json.dumps(env))
    http = FakeHttp()
    assert _post(path, http).code == "STATEMENT_SHAPE"
    assert http.calls == []


def test_deeply_nested_payload_does_not_crash_verify(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    env = json.loads(_bundle(tmp_path, key).read_text())
    env["payload"] = base64.b64encode(b"[" * 200_000).decode()
    path = tmp_path / "deep.json"
    path.write_text(json.dumps(env))
    result = verify_bundle(
        path, public_key_pem=_pem(tmp_path, key), repo=tmp_path, expected_candidate=CAND
    )
    assert not result.ok


@pytest.mark.parametrize(
    "exc", [http.client.IncompleteRead(b"x"), http.client.BadStatusLine("x"), TimeoutError()]
)
def test_http_client_failures_are_network_errors(tmp_path: Path, exc: Exception) -> None:
    res = _post(_bundle(tmp_path, Ed25519PrivateKey.generate()), FakeHttp(exc=exc))
    assert (res.ok, res.code) == (False, "NETWORK_ERROR")


def test_bad_key_file_is_not_reported_as_forgery(tmp_path: Path) -> None:
    path = _bundle(tmp_path, Ed25519PrivateKey.generate())
    garbage = tmp_path / "garbage.pem"
    garbage.write_text("not a key")
    http = FakeHttp()
    res = _post(path, http, public_key=garbage)
    assert (res.ok, res.code) == (False, "KEY_MALFORMED")
    missing = _post(path, http, public_key=tmp_path / "missing.pem")
    assert (missing.ok, missing.code) == (False, "KEY_UNREADABLE")
    assert http.calls == []


def test_repo_without_key_is_usage(tmp_path: Path) -> None:
    http = FakeHttp()
    res = _post(_bundle(tmp_path, Ed25519PrivateKey.generate()), http, repo=tmp_path)
    assert res.code == "USAGE" and http.calls == []


def test_full_verify_mode_reports_verify_bundle_code(tmp_path: Path, monkeypatch) -> None:
    key = Ed25519PrivateKey.generate()
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    seen = {}

    def spy(path, **kw):
        seen.update(kw)
        return verify_bundle(path, **kw)

    monkeypatch.setattr(checkrun, "verify_bundle", spy)
    http = FakeHttp()
    res = _post(_bundle(tmp_path, key), http, public_key=_pem(tmp_path, key), repo=repo)
    assert res.ok
    assert seen["repo"] == repo and seen["expected_candidate"] == CAND
    summary = _summary(http)
    line = next(x for x in summary.splitlines() if x.startswith("- Offline verification"))
    assert "VERIFIED" not in line and "signature only" not in line


def test_unverified_title_without_key(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    http = FakeHttp()
    _post(_bundle(tmp_path, key), http)
    assert "UNVERIFIED" in json.loads(http.calls[0][3])["output"]["title"]
    http2 = FakeHttp()
    _post(_bundle(tmp_path, key), http2, public_key=_pem(tmp_path, key))
    assert "UNVERIFIED" not in json.loads(http2.calls[0][3])["output"]["title"]


def test_free_text_fields_never_echoed(tmp_path: Path) -> None:
    evil = "see https://evil.example/x @octocat"
    path = _bundle(
        tmp_path, Ed25519PrivateKey.generate(), {"base_commit": evil, "test_source": evil}
    )
    http = FakeHttp()
    _post(path, http, name="probe @octocat")
    summary = _summary(http)
    assert "evil.example" not in summary and "@" not in summary
    assert "- Base: invalid" in summary and "- Test source: invalid" in summary
    assert "@" not in json.loads(http.calls[0][3])["name"]


def test_http_error_detail_never_contains_token(tmp_path: Path) -> None:
    http = FakeHttp(401, {"message": f"Bad credentials {TOKEN}"})
    res = _post(_bundle(tmp_path, Ed25519PrivateKey.generate()), http)
    assert res.code == "HTTP_ERROR"
    assert TOKEN[4:] not in res.detail


def test_cli_created_without_html_url(tmp_path: Path, capsys, monkeypatch) -> None:
    path = _bundle(tmp_path, Ed25519PrivateKey.generate())
    monkeypatch.setenv(checkrun.TOKEN_ENV, TOKEN)
    monkeypatch.setattr(checkrun, "_default_http", FakeHttp(body={"id": 1}))
    rc = main(["check-run", "--bundle", str(path), "--repo-slug", "octo/repo"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == f"CREATED {CAND} -"


@pytest.mark.parametrize(
    "kw",
    [
        {"token": "abc\r\nX-Evil: 1"},
        {"token": "tok en"},
        {"details_url": "https://"},
        {"details_url": "https:///path"},
    ],
)
def test_more_usage_errors(tmp_path: Path, kw: dict) -> None:
    http = FakeHttp()
    res = _post(_bundle(tmp_path, Ed25519PrivateKey.generate()), http, **kw)
    assert (res.ok, res.code) == (False, "USAGE")
    assert http.calls == [] and "abc" not in (res.detail or "")
