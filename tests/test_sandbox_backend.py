"""The sandbox backend interface (cairnwork_sandbox) and the Seatbelt profile bytes.

The profile hashes below are pinned as of the POSIX-semaphore change (#116),
with the same fixed inputs: building a profile does not need sandbox-exec.
"""

import importlib.util
from pathlib import Path

import pytest

import cairnwork_sandbox
from cairnwork.verifier_account import VerifierAccount, VerifierAccountError
from cairnwork_sandbox import seatbelt

HOME = Path("/Users/fixture")
READS = (
    Path("/opt/fixture/python3.12"),
    Path("/Users/fixture/run/private/pytest.ini"),
    Path("/Users/fixture/run/private/guard"),
)
WRITES = (Path("/Users/fixture/run/attempt"), Path("/Users/fixture/run/private/run"))
PINNED = {
    (): ("db6d79c085c4073a1fcc8173b5bf5de888a0d6cd8c1a15aa077dba50f0b6e08b", 1435),
    ("/Library/Developer/CommandLineTools",): (
        "9938948b6dbbb4ec7e891d3ed9ef0468577f3971f7ac61ba23dc6f078d568faf",
        1485,
    ),
    ("/Applications/Xcode.app",): (
        "6a1daab4ca02eaa34986ca53d92cd32f1dc9edf14c8c7a91c25378fac5190f2f",
        1533,
    ),
}


@pytest.mark.parametrize("dirs", list(PINNED))
def test_seatbelt_profile_is_pinned_as_of_the_posix_semaphore_change_116(monkeypatch, dirs):
    """Profile is pinned as of the POSIX-semaphore change (#116)."""
    monkeypatch.setattr(seatbelt, "developer_dirs", lambda: dirs)
    for profile in (
        seatbelt.build_profile(READS, WRITES, home=HOME),
        seatbelt.BACKEND.policy(READS, WRITES, home=HOME),
    ):
        assert (profile.sha256, len(profile.text)) == PINNED[dirs]


def test_seatbelt_argv_prefixes_are_unchanged(tmp_path):
    policy = tmp_path / "profile.sb"
    assert seatbelt.BACKEND.wrap(["/bin/true"], policy) == [
        "/usr/bin/sandbox-exec", "-f", str(policy), "/bin/true",
    ]  # fmt: skip
    assert seatbelt.BACKEND.unconfined(["/bin/true"]) == [
        "/usr/bin/sandbox-exec", "-p", "(version 1) (allow default)", "/bin/true",
    ]  # fmt: skip


def test_verifier_account_argv_is_unchanged(tmp_path, monkeypatch):
    import cairnwork.verifier_account as module

    monkeypatch.setattr(module, "platform_backend", lambda: seatbelt.BACKEND)
    account = VerifierAccount(user="u")
    head = ["/usr/bin/sudo", "-n", "-u", "u", "--"]
    env = ["/usr/bin/env", "-i", "A=1"]
    assert account.wrap(["x"], tmp_path / "p.sb", {"A": "1"}) == [
        *head, "/usr/bin/sandbox-exec", "-f", str(tmp_path / "p.sb"), *env, "x",
    ]  # fmt: skip
    assert account.wrap(["x"], None, {"A": "1"}) == [
        *head, "/usr/bin/sandbox-exec", "-p", "(version 1) (allow default)", *env, "x",
    ]  # fmt: skip


def test_no_backend_fails_closed(monkeypatch):
    monkeypatch.setattr(cairnwork_sandbox.sys, "platform", "win32")
    assert cairnwork_sandbox.platform_backend() is None
    assert cairnwork_sandbox.select() is None
    import cairnwork.verifier_account as module

    monkeypatch.setattr(module, "platform_backend", lambda: None)
    with pytest.raises(VerifierAccountError):
        VerifierAccount(user="u").wrap(["x"], None)


def test_select_refuses_a_backend_whose_probe_fails(monkeypatch):
    monkeypatch.setattr(cairnwork_sandbox.sys, "platform", "darwin")
    monkeypatch.setattr(seatbelt, "available", lambda: False)
    assert cairnwork_sandbox.platform_backend() is seatbelt.BACKEND
    assert cairnwork_sandbox.select() is None


def test_sandbox_package_has_its_own_size_cap(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("check_size", root / "scripts/check_size.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    package = tmp_path / "src" / "cairnwork_sandbox"
    package.mkdir(parents=True)
    for i in range(4):
        (package / f"m{i}.py").write_text("x = 1\n" * 301, encoding="utf-8")
    monkeypatch.setattr(module, "SANDBOX", package)
    monkeypatch.setattr(module, "SCANNED", [tmp_path / "src"])
    assert any("cairnwork_sandbox total 1204" in p for p in module.check(tmp_path))
