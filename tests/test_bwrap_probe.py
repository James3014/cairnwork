"""bwrap backend: the availability probe fails closed when bwrap is absent, and the policy and
argv construction (pure functions) are checked on any host. The real confinement is witnessed
by tests/test_sandbox.py on the test-linux-sandbox CI job."""

import sys
from pathlib import Path

import pytest

from cairnwork_sandbox import bwrap, platform_backend
from cairnwork_sandbox.backend import SandboxError


def test_available_is_false_without_bwrap(monkeypatch):
    monkeypatch.setattr(bwrap, "bwrap_path", lambda: None)
    bwrap.available.cache_clear()
    try:
        assert bwrap.available() is False
        assert bwrap.BACKEND.available() is False
    finally:
        bwrap.available.cache_clear()


def test_relative_bwrap_path_is_refused(monkeypatch):
    # A relative PATH entry would make wrap() run a candidate-planted ./bin/bwrap (Popen uses
    # cwd=checkout). bwrap_path must fail closed rather than hand back a relative program.
    monkeypatch.setattr(bwrap.shutil, "which", lambda _name: "bin/bwrap")
    bwrap.bwrap_path.cache_clear()
    try:
        assert bwrap.bwrap_path() is None
    finally:
        bwrap.bwrap_path.cache_clear()


def test_wrap_and_unconfined_fail_closed_without_bwrap(monkeypatch):
    monkeypatch.setattr(bwrap, "bwrap_path", lambda: None)
    with pytest.raises(SandboxError):
        bwrap.BACKEND.unconfined(["x"])
    p = Path("/nonexistent-policy")
    with pytest.raises(SandboxError):
        bwrap.BACKEND.wrap(["x"], p)


def test_linux_selects_the_bwrap_backend(monkeypatch):
    import cairnwork_sandbox

    monkeypatch.setattr(cairnwork_sandbox.sys, "platform", "linux")
    assert platform_backend() is bwrap.BACKEND
    assert sys.platform  # keep the import used


def _home(tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return home


def test_policy_is_read_only_root_plus_write_binds_then_credential_masks(tmp_path):
    home, work = _home(tmp_path), tmp_path / "work"
    work.mkdir()
    # Only existing credential paths are masked, so create each one first.
    for rel in bwrap._CRED_DIRS:
        (home / rel).mkdir(parents=True, exist_ok=True)
    for rel in bwrap._CRED_FILES:
        (home / rel).write_text("secret\n")
    lines = bwrap.BACKEND.policy((), (work,), home=home).text.splitlines()
    real_work, real_home = str(work.resolve()), str(home.resolve())
    assert lines[:3] == ["--unshare-all", "--unshare-user", "--new-session"]
    assert "--cap-drop" in lines and "--disable-userns" in lines
    root = lines.index("--ro-bind")
    assert lines[root : root + 3] == ["--ro-bind", "/", "/"]
    bind = lines.index("--bind")
    assert lines[bind : bind + 3] == ["--bind", real_work, real_work]
    assert bind > root  # later flags override: write bind comes after the read-only root
    for rel in bwrap._CRED_DIRS:
        tmpfs = lines.index(f"{real_home}/{rel}")
        assert lines[tmpfs - 1] == "--tmpfs" and tmpfs > bind
    netrc = lines.index(f"{real_home}/.netrc")
    assert lines[netrc - 2 : netrc + 1] == ["--ro-bind", "/dev/null", f"{real_home}/.netrc"]


def test_dangerous_sockets_are_masked_only_when_present(tmp_path, monkeypatch):
    # A reachable Docker/Podman/containerd socket is root-equivalent and a read-only root bind
    # does not stop connect(), so an existing one is replaced with /dev/null; an absent one adds
    # no flag (a bind over a missing path would fail on the read-only root).
    home = _home(tmp_path)
    present = tmp_path / "docker.sock"
    present.write_text("")  # stands in for the socket inode
    absent = tmp_path / "podman.sock"
    monkeypatch.setattr(bwrap, "_DANGEROUS_SOCKETS", (str(present), str(absent)))
    lines = bwrap.BACKEND.policy((), (), home=home).text.splitlines()
    masked = lines.index(str(present))
    assert lines[masked - 2 : masked + 1] == ["--ro-bind", "/dev/null", str(present)]
    assert str(absent) not in lines


def test_absent_credentials_are_not_masked(tmp_path):
    # Masking applies only to paths that exist, so an empty home adds no credential masks (a
    # tmpfs over a non-existent path fails on top of the read-only root).
    home = _home(tmp_path)
    lines = bwrap.BACKEND.policy((), (), home=home).text.splitlines()
    assert "--tmpfs" not in lines
    assert f"{home.resolve()}/.netrc" not in lines


def test_wrap_reads_the_policy_file(monkeypatch, tmp_path):
    monkeypatch.setattr(bwrap, "bwrap_path", lambda: "/usr/bin/bwrap")
    path = tmp_path / "p"
    path.write_text("--unshare-all\n--tmpfs\n/x\n")
    assert bwrap.BACKEND.wrap(["a", "b"], path) == [
        "/usr/bin/bwrap", "--unshare-all", "--tmpfs", "/x", "--", "a", "b",
    ]  # fmt: skip
    assert bwrap.BACKEND.unconfined(["a"]) == [
        "/usr/bin/bwrap", "--bind", "/", "/", "--dev", "/dev", "--", "a",
    ]  # fmt: skip


@pytest.mark.parametrize(
    "bad", ['/tmp/a"b', "/tmp/a(b", "/tmp/a\\b", "/tmp/a\nb", "/tmp/\u00e9", "/tmp/a\n"]
)
def test_policy_paths_cannot_add_lines(tmp_path, bad):
    with pytest.raises(SandboxError):
        bwrap._safe(bad)
    with pytest.raises(SandboxError):
        bwrap.BACKEND.policy((Path(bad),), (), home=_home(tmp_path))
    with pytest.raises(SandboxError):
        bwrap.BACKEND.policy((), (Path(bad),), home=_home(tmp_path))


@pytest.mark.parametrize(
    "root",
    ["/", "/home", "/tmp", "/var", "/var/tmp", "/usr", "/etc", "HOME", "HOME_PARENT"],  # noqa: S108
)
def test_broad_write_roots_are_refused(tmp_path, root):
    import os

    if root[0] == "/" and os.path.realpath(root) != root:
        pytest.skip(f"{root} is a symlink on this host (macOS); its real path is not listed")
    home = _home(tmp_path)
    path = {"HOME": home, "HOME_PARENT": tmp_path}.get(root, root)
    with pytest.raises(SandboxError):
        bwrap.BACKEND.policy((), (Path(path),), home=home)
