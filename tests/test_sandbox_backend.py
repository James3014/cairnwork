"""The sandbox backend interface (cairnwork_sandbox) and the move of Seatbelt out of the core.

The profile hashes below were computed on origin/main at 3a91e76, before Seatbelt moved
from src/cairnwork/sandbox.py, with the same fixed inputs: the move must not change one
byte of the policy (its sha256 also enters the verifier observation digest).
These run everywhere: building a profile does not need sandbox-exec.
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
    (): ("586f2f89c28b51225d8e3d59f758be7f9bb15022c9bc357d6c7bc07b440c6e39", 1381),
    ("/Library/Developer/CommandLineTools",): (
        "e10da2ac22cc5e42500c1ff126640aee0074df9b58e4f89b2404b25e4194f70e",
        1431,
    ),
    ("/Applications/Xcode.app",): (
        "b7e076ce7085147ec6342005a57087295ddee4d2ba1b92aee965a0551f7fec83",
        1479,
    ),
}


@pytest.mark.parametrize("dirs", list(PINNED))
def test_seatbelt_profile_is_byte_identical_to_before_the_move(monkeypatch, dirs):
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
    monkeypatch.setattr(cairnwork_sandbox.sys, "platform", "linux")
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
