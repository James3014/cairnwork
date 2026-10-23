"""Credentials in remote URLs that git echoes on stderr never reach a publish result."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from publish_helpers import _make_bundle_bytes, _write_bundle, make_publish_env

from cairnwork_attest.publish import _git, _redact, publish_bundle

SECRET = "ghs_SeCrEt0123456789"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            f"To https://x-access-token:{SECRET}@github.com/o/r.git",
            "To https://***@github.com/o/r.git",
        ),
        (
            f"fatal: unable to access 'https://{SECRET}@host/r/'",
            "fatal: unable to access 'https://***@host/r/'",
        ),
        ("error: failed to push some refs to 'https://github.com/o/r.git'", None),
        ("git@github.com:o/r.git rejected", None),
        ("https://user:p@ss@host/r.git", "https://***@host/r.git"),
        (f"HTTPS://{SECRET}@host/r", "HTTPS://***@host/r"),
        (f"https://{SECRET}@[::1]:8080/r.git", "https://***@[::1]:8080/r.git"),
        ("https://host/repo@v1 and mail a@b.com", None),
        (
            f"To https://a:{SECRET}@h/x\nfatal: https://b:{SECRET}@h/y",
            "To https://***@h/x\nfatal: https://***@h/y",
        ),
    ],
)
def test_redact(text: str, expected: str | None) -> None:
    assert _redact(text) == (text if expected is None else expected)


@pytest.mark.parametrize("failing", ["fetch", "push", "push+restore"])
def test_publish_failure_detail_hides_url_credentials(tmp_path: Path, failing: str) -> None:
    src, _bare, candidate = make_publish_env(tmp_path)
    url = f"https://x-access-token:{SECRET}@github.com/o/r.git"

    def run(repo: Path, *args: str, input: bytes | None = None):  # noqa: ANN202
        if args[0] == "fetch" and failing == "fetch":
            return subprocess.CompletedProcess(args, 128, "", f"fatal: unable to access '{url}'\n")
        if args[0] == "push" and failing.startswith("push"):
            return subprocess.CompletedProcess(args, 1, "", f"To {url}\n ! [remote rejected] x\n")
        if args[0] == "update-ref" and failing == "push+restore":
            return subprocess.CompletedProcess(args, 1, "", f"cannot lock ref via {url}\n")
        return _git(repo, *args, input=input)

    bundle = _write_bundle(tmp_path, _make_bundle_bytes(candidate, Ed25519PrivateKey.generate()))
    r = publish_bundle(bundle, src, candidate, run=run)
    assert r.ok is False
    code = {
        "fetch": "FETCH_FAILED",
        "push": "PUSH_FAILED",
        "push+restore": "LOCAL_REF_RESET_FAILED",
    }
    assert r.code == code[failing]
    assert SECRET not in (r.detail or "") and "***@github.com" in (r.detail or "")


def test_a_url_passed_as_remote_is_redacted_in_the_usage_error(tmp_path: Path) -> None:
    src, _bare, candidate = make_publish_env(tmp_path)
    bundle = _write_bundle(tmp_path, _make_bundle_bytes(candidate, Ed25519PrivateKey.generate()))
    r = publish_bundle(bundle, src, candidate, remote=f"https://{SECRET}@github.com/o/r.git")
    assert r.code == "USAGE" and SECRET not in (r.detail or "") and SECRET not in r.remote
