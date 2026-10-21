"""bwrap backend: the availability probe fails closed when bwrap is absent, and the policy and
argv construction (pure functions) are checked on any host. The real confinement is witnessed
by tests/test_sandbox.py on the test-linux-sandbox CI job."""

import sys
from pathlib import Path

import pytest

from cairnwork_sandbox import bwrap, platform_backend
from cairnwork_sandbox.backend import SandboxError


@pytest.fixture(autouse=True)
def _host_independent_ipc_masks(monkeypatch):
    """Keep the pure-function policy tests independent of the test host: the daemon-socket and
    /run/user masks fire on paths that really exist on the runner (the system D-Bus socket, the
    runtime tree), which would otherwise add host-dependent flags. Tests that exercise those
    masks re-enable them in their own body. Only /run/user is forced absent, so pytest's own
    isdir calls are untouched (patching os.getuid globally breaks its tmp-owner check)."""
    import os

    real_isdir = os.path.isdir
    monkeypatch.setattr(bwrap, "_DANGEROUS_SOCKETS", ())
    monkeypatch.setattr(
        bwrap.os.path, "isdir", lambda p: False if p == "/run/user" else real_isdir(p)
    )


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


def test_home_credential_masks_are_skipped_when_asked(tmp_path):
    # Under the dedicated account (#84) the sandbox runs as a uid that cannot enter the owner's
    # 0700 home, so those tmpfs mounts would EPERM; mask_home_credentials=False skips them. The
    # narrow-check still uses the real home, and the write bind is unaffected.
    home, work = _home(tmp_path), tmp_path / "work"
    work.mkdir()
    for rel in bwrap._CRED_DIRS:
        (home / rel).mkdir(parents=True, exist_ok=True)
    for rel in bwrap._CRED_FILES:
        (home / rel).write_text("secret\n")
    lines = bwrap.BACKEND.policy((), (work,), home=home, mask_home_credentials=False).text
    real_home = str(home.resolve())
    assert f"{real_home}/.ssh" not in lines and f"{real_home}/.netrc" not in lines
    assert "--tmpfs" not in lines.splitlines()  # the autouse fixture also clears /run/user
    assert str(work.resolve()) in lines  # the write bind is still there


def test_dangerous_sockets_are_masked_only_when_present(tmp_path, monkeypatch):
    # A reachable Docker/Podman/containerd socket is root-equivalent and a read-only root bind
    # does not stop connect(), so an existing one is replaced with /dev/null at its canonical
    # path; an absent one adds no flag (a bind over a missing path would fail on the read-only
    # root), and duplicate paths (the /var/run -> /run symlink) collapse to one.
    import os

    home = _home(tmp_path)
    present = tmp_path / "docker.sock"
    present.write_text("")  # stands in for the socket inode
    real_present = os.path.realpath(present)
    absent = tmp_path / "podman.sock"
    monkeypatch.setattr(bwrap, "_DANGEROUS_SOCKETS", (str(present), real_present, str(absent)))
    lines = bwrap.BACKEND.policy((), (), home=home).text.splitlines()
    masked = lines.index(real_present)
    assert lines[masked - 2 : masked + 1] == ["--ro-bind", "/dev/null", real_present]
    assert lines.count(real_present) == 1  # the duplicate entry collapsed
    assert str(absent) not in lines and os.path.realpath(absent) not in lines


def test_user_runtime_dir_is_masked_with_tmpfs(tmp_path, monkeypatch):
    # The whole /run/user tree (systemd user bus — a systemd-run --user escape — and session
    # agent sockets) is emptied with a tmpfs, for every uid at once. Masked only when it exists.
    monkeypatch.setattr(bwrap, "_user_runtime_dir", lambda: "/run/user")
    lines = bwrap.BACKEND.policy((), (), home=_home(tmp_path)).text.splitlines()
    i = lines.index("/run/user")
    assert lines[i - 1] == "--tmpfs"


def test_user_runtime_dir_masks_the_whole_tree_not_one_uid(monkeypatch):
    # Regression (round-3 review): the tree, not /run/user/<launcher-uid>, so a P4 account uid's
    # /run/user/<uid>/bus is covered too. The helper must not depend on os.getuid().
    monkeypatch.setattr(bwrap.os.path, "isdir", lambda p: p == "/run/user")
    assert bwrap._user_runtime_dir() == "/run/user"


def test_user_runtime_dir_absent_adds_no_tmpfs(tmp_path, monkeypatch):
    monkeypatch.setattr(bwrap, "_user_runtime_dir", lambda: None)
    lines = bwrap.BACKEND.policy((), (), home=_home(tmp_path)).text.splitlines()
    assert "--tmpfs" not in lines


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
