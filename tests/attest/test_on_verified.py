"""Tests for cairnwork-attest on-verified (no network; local bare repo as origin)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from conftest import git
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from publish_helpers import _make_bundle_bytes_alt, _remote_tip

from cairnwork_attest import checkrun
from cairnwork_attest.cli import main
from cairnwork_attest.publish import publish_bundle
from tests.attest.test_attest_cli import setup_cli_fixture

TOKEN = "ghs_SECRETTOKEN123"
SLUG = "octo/repo"


class Env:
    def __init__(self, tmp_path: Path, repo: Path) -> None:
        self.state = tmp_path / "state"
        self.repo = repo
        self.candidate = setup_cli_fixture(self.state, repo)
        main(["keygen", "--state", str(self.state)])
        self.bare = tmp_path / "bare.git"
        git(tmp_path, "clone", "-q", "--bare", str(repo), str(self.bare))
        git(repo, "remote", "add", "origin", str(self.bare))
        self.bundle = self.state / "attestations" / "task-1-a1.bundle.json"

    def argv(self, *extra: str, **over: str) -> list[str]:
        base = {
            "--state": str(self.state),
            "--task-id": "task-1",
            "--attempt-id": "a1",
            "--candidate": self.candidate,
            "--repo": str(self.repo),
            "--repo-slug": SLUG,
        }
        base.update(over)
        flat = [x for kv in base.items() for x in kv]
        return ["on-verified", *flat, *extra]


class FakeHttp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str], bytes]] = []

    def __call__(self, url, method, headers, body) -> tuple[int, bytes]:
        self.calls.append((url, method, headers, body))
        return 201, json.dumps({"id": 7, "html_url": "https://x/7"}).encode()


@pytest.fixture
def env(tmp_path: Path, repo: Path) -> Env:
    return Env(tmp_path, repo)


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch) -> FakeHttp:
    fake = FakeHttp()
    monkeypatch.setattr(checkrun, "_default_http", fake)
    monkeypatch.setenv(checkrun.TOKEN_ENV, TOKEN)
    return fake


def _run(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, str, dict]:
    rc = main(argv)
    cap = capsys.readouterr()
    assert cap.out.count("\n") == 1
    return rc, cap.out, json.loads(cap.out)


def _codes(obj: dict) -> list[tuple[str, str]]:
    return [(s["step"], s["code"]) for s in obj["steps"]]


def test_happy_path_exact_json(
    env: Env, http: FakeHttp, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out, obj = _run(capsys, env.argv())
    expected = {
        "attempt_id": "a1",
        "bundle": str(env.bundle),
        "candidate": env.candidate,
        "ok": True,
        "steps": [
            {"code": "OK", "detail": None, "step": "statement"},
            {"code": "PUBLISHED", "detail": None, "step": "publish"},
            {"code": "CREATED", "detail": None, "step": "check_run"},
        ],
        "task_id": "task-1",
    }
    assert rc == 0
    assert out == json.dumps(expected, sort_keys=True, separators=(",", ":")) + "\n"
    assert _remote_tip(env.bare) is not None
    assert len(http.calls) == 1
    assert http.calls[0][0] == "https://api.github.com/repos/octo/repo/check-runs"
    assert json.loads(http.calls[0][3])["head_sha"] == env.candidate


def test_bundle_mode_is_0600(env: Env, http: FakeHttp, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(capsys, env.argv())[0] == 0
    assert stat.S_IMODE(env.bundle.stat().st_mode) == 0o600


def test_second_run_is_noop_and_reuses_bytes(
    env: Env, http: FakeHttp, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(capsys, env.argv())[0] == 0
    first = env.bundle.read_bytes()
    rc, _, obj = _run(capsys, env.argv())
    assert rc == 0
    assert _codes(obj) == [("statement", "OK"), ("publish", "NOOP"), ("check_run", "CREATED")]
    assert env.bundle.read_bytes() == first


def test_different_existing_bundle_fails_bundle_exists(
    env: Env, http: FakeHttp, capsys: pytest.CaptureFixture[str]
) -> None:
    env.bundle.parent.mkdir(parents=True)
    env.bundle.write_text("{}")
    rc, _, obj = _run(capsys, env.argv())
    assert rc == 2
    assert _codes(obj) == [
        ("statement", "BUNDLE_EXISTS"),
        ("publish", "SKIPPED"),
        ("check_run", "SKIPPED"),
    ]
    assert env.bundle.read_text() == "{}"
    assert _remote_tip(env.bare) is None


def test_note_conflict_exits_1(
    env: Env, http: FakeHttp, capsys: pytest.CaptureFixture[str]
) -> None:
    other = env.bundle.parent.parent / "other.json"
    other.write_bytes(_make_bundle_bytes_alt(env.candidate, Ed25519PrivateKey.generate()))
    assert publish_bundle(other, env.repo, env.candidate).code == "PUBLISHED"
    rc, _, obj = _run(capsys, env.argv())
    assert rc == 1
    assert obj["ok"] is False
    assert _codes(obj) == [
        ("statement", "OK"),
        ("publish", "NOTE_CONFLICT"),
        ("check_run", "SKIPPED"),
    ]
    assert http.calls == []


def test_statement_failure_skips_rest(
    env: Env, http: FakeHttp, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, _, obj = _run(capsys, env.argv(**{"--task-id": "nope"}))
    assert rc == 2
    assert obj["bundle"] is None and obj["ok"] is False
    assert obj["steps"][0]["step"] == "statement" and obj["steps"][0]["code"] != "OK"
    assert _codes(obj)[1:] == [("publish", "SKIPPED"), ("check_run", "SKIPPED")]
    assert _remote_tip(env.bare) is None and http.calls == []


def test_missing_token_is_usage_but_publish_done(
    env: Env, http: FakeHttp, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(checkrun.TOKEN_ENV)
    rc, _, obj = _run(capsys, env.argv())
    assert rc == 2
    assert _codes(obj) == [("statement", "OK"), ("publish", "PUBLISHED"), ("check_run", "USAGE")]
    assert _remote_tip(env.bare) is not None and http.calls == []


def test_no_check_run(env: Env, http: FakeHttp, capsys: pytest.CaptureFixture[str]) -> None:
    rc, _, obj = _run(capsys, env.argv("--no-check-run"))
    assert rc == 0 and obj["ok"] is True
    assert _codes(obj) == [("statement", "OK"), ("publish", "PUBLISHED"), ("check_run", "SKIPPED")]
    assert http.calls == []


def test_token_never_in_output(
    env: Env, http: FakeHttp, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Leaky(FakeHttp):
        def __call__(self, url, method, headers, body) -> tuple[int, bytes]:
            return 403, json.dumps({"message": f"bad {TOKEN}"}).encode()

    monkeypatch.setattr(checkrun, "_default_http", Leaky())
    rc = main(env.argv())
    cap = capsys.readouterr()
    assert rc == 2
    assert TOKEN not in cap.out and TOKEN not in cap.err
    assert json.loads(cap.out)["steps"][2]["code"] == "HTTP_ERROR"
    assert TOKEN not in env.bundle.read_text()


@pytest.mark.parametrize(
    "over",
    [
        {"--task-id": "../evil"},
        {"--task-id": "a/b"},
        {"--task-id": ".hidden"},
        {"--task-id": ".."},
        {"--attempt-id": "x/../y"},
        {"--attempt-id": ""},
        {"--candidate": "A" * 40},
        {"--candidate": "abc"},
        {"--repo-slug": "noslash"},
        {"--repo-slug": "a/b/c"},
        {"--state": "relative/state"},
        {"--repo": "relative/repo"},
        {"--out-dir": "relative/out"},
    ],
)
def test_invalid_inputs_usage_no_side_effects(
    env: Env, http: FakeHttp, capsys: pytest.CaptureFixture[str], over: dict[str, str]
) -> None:
    rc, _, obj = _run(capsys, env.argv(**over))
    assert rc == 2
    assert obj["ok"] is False and obj["bundle"] is None
    assert _codes(obj) == [
        ("statement", "USAGE"),
        ("publish", "SKIPPED"),
        ("check_run", "SKIPPED"),
    ]
    assert not (env.state / "attestations").exists()
    assert _remote_tip(env.bare) is None and http.calls == []


def test_out_dir_is_used(
    env: Env, http: FakeHttp, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_dir = tmp_path / "elsewhere"
    rc, _, obj = _run(capsys, env.argv(**{"--out-dir": str(out_dir)}))
    assert rc == 0
    assert obj["bundle"] == str(out_dir / "task-1-a1.bundle.json")
    assert (out_dir / "task-1-a1.bundle.json").is_file()


def test_symlinked_bundle_path_is_refused(
    env: Env, http: FakeHttp, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(capsys, env.argv())[0] == 0
    real = env.bundle.parent.parent / "real.bundle.json"
    env.bundle.rename(real)
    env.bundle.symlink_to(real)  # a valid bundle, but reached through a link
    rc, _, obj = _run(capsys, env.argv())
    assert rc == 2
    assert _codes(obj)[0] == ("statement", "BUNDLE_EXISTS")
    assert env.bundle.is_symlink()
