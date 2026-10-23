"""Tests for cairnwork_attest.checkrun (no network)."""

from __future__ import annotations

import base64
import json
import urllib.error
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from cairnwork_attest.checkrun import post_check_run
from cairnwork_attest.cli import main
from cairnwork_attest.dsse import _sign_bytes
from cairnwork_attest.keys import keyid

CAND = "a" * 40
BASE = "b" * 40
TOKEN = "ghs_SECRETTOKEN123"
SLUG = "octo/repo"


class FakeHttp:
    def __init__(self, status: int = 201, body: dict | None = None, exc: Exception | None = None):
        self.status, self.exc = status, exc
        self.body = json.dumps(body if body is not None else {"id": 7, "html_url": "https://x/7"})
        self.calls: list[tuple[str, str, dict[str, str], bytes]] = []

    def __call__(self, url, method, headers, body):
        self.calls.append((url, method, headers, body))
        if self.exc:
            raise self.exc
        return self.status, self.body.encode()


def _bundle(tmp_path: Path, key: Ed25519PrivateKey, predicate: dict | None = None) -> Path:
    pred = {"base_commit": BASE, "test_source": "bundle"} if predicate is None else predicate
    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "predicateType": "https://cairnwork.github.io/attestation/verified-change/v0.1",
        "predicate": pred,
        "subject": [{"digest": {"gitCommit": CAND}, "name": f"git-commit:{CAND}"}],
    }
    payload = json.dumps(statement, sort_keys=True, separators=(",", ":")).encode()
    path = tmp_path / "b.json"
    path.write_text(json.dumps(_sign_bytes(payload, key)))
    return path


def _pem(tmp_path: Path, key: Ed25519PrivateKey) -> Path:
    path = tmp_path / "pub.pem"
    pem = key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    path.write_bytes(pem)
    return path


def _post(path: Path, http: FakeHttp, **kw):
    kw.setdefault("token", TOKEN)
    return post_check_run(path, SLUG, http=http, **kw)


def test_created_request_shape(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    http = FakeHttp()
    res = _post(_bundle(tmp_path, key), http)
    assert (res.ok, res.code, res.candidate) == (True, "CREATED", CAND)
    assert (res.check_run_id, res.html_url) == (7, "https://x/7")
    url, method, headers, body = http.calls[0]
    assert url == "https://api.github.com/repos/octo/repo/check-runs"
    assert method == "POST"
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["Accept"] == "application/vnd.github+json"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert headers["User-Agent"] == "cairnwork-attest"
    assert TOKEN.encode() not in body
    data = json.loads(body)
    assert data["head_sha"] == CAND and data["status"] == "completed"
    assert data["conclusion"] == "neutral" and data["name"] == "cairnwork attestation"
    assert "details_url" not in data
    summary = data["output"]["summary"]
    statement = base64.b64decode(json.loads((tmp_path / "b.json").read_text())["payload"])
    import hashlib

    for needle in (
        CAND,
        BASE,
        hashlib.sha256(statement).hexdigest(),
        "bundle",
        keyid(key.public_key()),
    ):
        assert needle in summary
    assert "signature not checked by this step" in summary
    assert "cairnwork-attest verify" in summary and "cairnwork/verify-action" in summary


def test_details_url_and_name_sent(tmp_path: Path) -> None:
    http = FakeHttp()
    _post(
        _bundle(tmp_path, Ed25519PrivateKey.generate()),
        http,
        details_url="https://e.x/y",
        name="n1",
    )
    data = json.loads(http.calls[0][3])
    assert data["details_url"] == "https://e.x/y" and data["name"] == "n1"


def test_public_key_verified_and_failure(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    path = _bundle(tmp_path, key)
    http = FakeHttp()
    _post(path, http, public_key=_pem(tmp_path, key))
    assert "VERIFIED, signature only" in json.loads(http.calls[0][3])["output"]["summary"]
    other = Ed25519PrivateKey.generate()
    http2 = FakeHttp()
    res = _post(path, http2, public_key=_pem(tmp_path, other))
    assert res.ok  # informational: still posted
    summary = json.loads(http2.calls[0][3])["output"]["summary"]
    assert "SIGNATURE_INVALID" in summary and "VERIFIED" not in summary


@pytest.mark.parametrize("status", [401, 422, 500])
def test_http_error_no_token_in_detail(tmp_path: Path, status: int) -> None:
    http = FakeHttp(status, {"message": f"bad <b>creds</b> {TOKEN}"[:60]})
    res = _post(_bundle(tmp_path, Ed25519PrivateKey.generate()), http)
    assert (res.ok, res.code) == (False, "HTTP_ERROR")
    assert res.detail.startswith(f"HTTP {status}")
    assert "<" not in res.detail and ">" not in res.detail
    http2 = FakeHttp(status, {"message": "nope"})
    res2 = _post(_bundle(tmp_path, Ed25519PrivateKey.generate()), http2)
    assert res2.detail == f"HTTP {status}: nope"


def test_network_error(tmp_path: Path) -> None:
    http = FakeHttp(exc=urllib.error.URLError(f"boom {TOKEN}"))
    res = _post(_bundle(tmp_path, Ed25519PrivateKey.generate()), http)
    assert (res.ok, res.code) == (False, "NETWORK_ERROR")
    assert TOKEN not in (res.detail or "")


@pytest.mark.parametrize(
    "kw",
    [
        {"token": None},
        {"token": ""},
        {"details_url": "http://insecure"},
        {"details_url": "https://a b"},
    ],
)
def test_usage_errors(tmp_path: Path, kw: dict) -> None:
    http = FakeHttp()
    res = _post(_bundle(tmp_path, Ed25519PrivateKey.generate()), http, **kw)
    assert (res.ok, res.code) == (False, "USAGE")
    assert http.calls == []


@pytest.mark.parametrize("slug", ["x", "a/b/c", "../b", "a/..", "./b", "a b/c", "a/b;", "/b"])
def test_bad_slug(tmp_path: Path, slug: str) -> None:
    http = FakeHttp()
    res = post_check_run(
        _bundle(tmp_path, Ed25519PrivateKey.generate()), slug, token=TOKEN, http=http
    )
    assert res.code == "USAGE" and http.calls == []


def test_bundle_errors(tmp_path: Path) -> None:
    http = FakeHttp()
    missing = _post(tmp_path / "nope.json", http)
    assert missing.code == "BUNDLE_NOT_FOUND"
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert _post(bad, http).code == "BUNDLE_MALFORMED"
    env = json.loads(_bundle(tmp_path, Ed25519PrivateKey.generate()).read_text())
    env["payload"] = base64.b64encode(
        json.dumps({"subject": [{"digest": {"gitCommit": "XYZ"}}]}).encode()
    ).decode()
    bad.write_text(json.dumps(env))
    assert _post(bad, http).code == "STATEMENT_SHAPE"
    env["payload"] = "!!!notb64"
    bad.write_text(json.dumps(env))
    assert _post(bad, http).code == "STATEMENT_SHAPE"
    assert http.calls == []


def test_tampered_payload_flagged_with_key(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    env = json.loads(_bundle(tmp_path, key).read_text())
    stmt = json.loads(base64.b64decode(env["payload"]))
    stmt["predicate"]["base_commit"] = "c" * 40
    env["payload"] = base64.b64encode(json.dumps(stmt).encode()).decode()
    path = tmp_path / "t.json"
    path.write_text(json.dumps(env))
    http = FakeHttp()
    _post(path, http, public_key=_pem(tmp_path, key))
    assert "SIGNATURE_INVALID" in json.loads(http.calls[0][3])["output"]["summary"]


def test_injection_neutralised(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    evil = "<img src=x onerror=1>`x`\n# h [l](http://e) " + "A" * 500
    path = _bundle(tmp_path, key, {"base_commit": evil, "test_source": evil})
    http = FakeHttp()
    _post(path, http, name=evil)
    data = json.loads(http.calls[0][3])
    summary = data["output"]["summary"]
    assert "<img" not in summary and "onerror=1>" not in summary
    assert "[l]" not in summary and "A" * 201 not in summary
    assert summary.count("`") == 2  # only our own `cairnwork-attest verify`
    assert "\n# h" not in summary
    assert len(data["name"]) <= 200 and "<" not in data["name"]


def test_cli_json(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch) -> None:
    import cairnwork_attest.checkrun as mod

    path = _bundle(tmp_path, Ed25519PrivateKey.generate())
    http = FakeHttp()
    monkeypatch.setattr(mod, "_default_http", http)
    monkeypatch.setenv("CAIRNWORK_GITHUB_TOKEN", TOKEN)
    rc = main(["check-run", "--bundle", str(path), "--repo-slug", SLUG, "--json"])
    out = capsys.readouterr()
    assert rc == 0
    assert json.loads(out.out) == {
        "candidate": CAND,
        "check_run_id": 7,
        "code": "CREATED",
        "detail": None,
        "html_url": "https://x/7",
        "ok": True,
    }
    assert TOKEN not in out.out + out.err
    assert http.calls[0][2]["Authorization"] == f"Bearer {TOKEN}"


def test_cli_missing_token(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch) -> None:
    monkeypatch.delenv("CAIRNWORK_GITHUB_TOKEN", raising=False)
    path = _bundle(tmp_path, Ed25519PrivateKey.generate())
    rc = main(["check-run", "--bundle", str(path), "--repo-slug", SLUG, "--json"])
    assert rc == 2
    assert json.loads(capsys.readouterr().out)["code"] == "USAGE"
