"""bwrap backend: the availability probe fails closed when bwrap is absent, and the policy and
argv construction (pure functions) are checked on any host. The real confinement is witnessed
by tests/test_sandbox.py on the test-linux-sandbox CI job."""

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from cairnwork_sandbox import bwrap, platform_backend
from cairnwork_sandbox.backend import SandboxError


@pytest.fixture
def world_home():
    """A home whose ancestors are world-traversable, so the account-path reachability walk (which
    runs from ``/``) can pass. pytest's own tmp tree is 0700, which would block it; ``/tmp`` is
    1777. The home itself is 0755 unless a test tightens it."""
    base = Path(tempfile.mkdtemp(dir="/tmp", prefix="cw-home-"))  # noqa: S108 - needs world ancestors
    os.chmod(base, 0o755)
    try:
        yield base
    finally:
        for root, dirs, _ in os.walk(base):  # some tests make 0700 subdirs; reopen them to delete
            for d in dirs:
                os.chmod(os.path.join(root, d), 0o700)
        shutil.rmtree(base, ignore_errors=True)


# Captured before the autouse fixture neutralises the module attribute, so a test can check the
# real masked-socket list.
_REAL_DANGEROUS_SOCKETS = tuple(bwrap._DANGEROUS_SOCKETS)


@pytest.fixture(autouse=True)
def _host_independent_ipc_masks(monkeypatch):
    """Keep the pure-function policy tests independent of the test host: the daemon-socket and
    /run/user masks fire on paths that really exist on the runner (the system D-Bus socket, the
    runtime tree), which would otherwise add host-dependent flags. Tests that exercise those
    masks re-enable them in their own body. Only /run/user is forced absent, so pytest's own
    isdir calls are untouched (patching os.getuid globally breaks its tmp-owner check)."""
    import os

    real_isdir = os.path.isdir
    masked_dirs = {"/run/user", bwrap._X11_SOCKET_DIR, "/run/libvirt", "/var/run/libvirt"}
    monkeypatch.setattr(bwrap, "_DANGEROUS_SOCKETS", ())
    monkeypatch.setattr(
        bwrap.os.path, "isdir", lambda p: False if p in masked_dirs else real_isdir(p)
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


def test_home_credential_masks_are_skipped_under_a_private_home(tmp_path):
    # Under the dedicated account (#84) the sandbox runs as a uid that cannot enter the owner's
    # 0700 home, so those tmpfs mounts would EPERM; mask_home_credentials=False then masks nothing.
    # The narrow-check still uses the real home, and the write bind is unaffected.
    home, work = _home(tmp_path), tmp_path / "work"
    work.mkdir()
    for rel in bwrap._CRED_DIRS:
        (home / rel).mkdir(parents=True, exist_ok=True)
    for rel in bwrap._CRED_FILES:
        (home / rel).write_text("secret\n")
    home.chmod(0o700)  # a correctly private home: the account uid cannot enter it
    lines = bwrap.BACKEND.policy((), (work,), home=home, mask_home_credentials=False).text
    real_home = str(home.resolve())
    assert f"{real_home}/.ssh" not in lines and f"{real_home}/.netrc" not in lines
    assert "--tmpfs" not in lines.splitlines()  # the autouse fixture also clears /run/user
    assert str(work.resolve()) in lines  # the write bind is still there


def test_reachable_credentials_are_masked_under_the_account(world_home):
    # Issue #93: on a loosened (0755) owner home the account can resolve credential paths, so a
    # world-readable one still leaks. mask_home_credentials=False masks every path the account can
    # reach to mount over — read bits are not consulted: masking a reachable path is harmless, and
    # a 0711 directory is unreadable yet still lets a known world-readable child under it leak.
    home = world_home
    work = home / "work"
    work.mkdir()
    (home / ".netrc").write_text("secret\n")
    (home / ".netrc").chmod(0o644)  # world-readable file
    (home / ".git-credentials").write_text("token\n")
    (home / ".git-credentials").chmod(0o600)  # owner-only file, but still reachable → masked
    (home / ".ssh").mkdir()
    (home / ".ssh").chmod(0o711)  # not listable, but a known child under it would leak (finding)
    policy = bwrap.BACKEND.policy((), (work,), home=home, mask_home_credentials=False).text
    lines = policy.splitlines()
    real_home = str(home.resolve())
    for f in (".netrc", ".git-credentials"):
        i = lines.index(f"{real_home}/{f}")
        assert lines[i - 2 : i + 1] == ["--ro-bind", "/dev/null", f"{real_home}/{f}"]
    ssh = lines.index(f"{real_home}/.ssh")
    assert lines[ssh - 1] == "--tmpfs"  # the 0711 directory is masked, not skipped


def test_a_credential_behind_a_private_ancestor_is_not_masked(world_home):
    # Reachability, not the target's own perms, decides masking: a path the account cannot resolve
    # (an ancestor it cannot enter, or a symlink that lands behind one) must not be masked — binding
    # over it would EPERM and fail the run. A reachable one alongside it is still masked.
    home = world_home
    work = home / "work"
    work.mkdir()
    (home / ".local").mkdir()
    (home / ".local").chmod(0o700)  # private intermediate: the account cannot enter it
    (home / ".local" / "state").mkdir()  # a _CRED_DIRS path now unreachable through .local
    (home / "priv").mkdir()
    (home / "priv").chmod(0o700)
    (home / "priv" / "secret").write_text("token\n")
    (home / "priv" / "secret").chmod(0o644)
    (home / ".netrc").symlink_to(home / "priv" / "secret")  # resolves behind a 0700 dir
    (home / ".npmrc").write_text("//r/:_authToken=x\n")
    (home / ".npmrc").chmod(0o644)  # reachable control
    lines = bwrap.BACKEND.policy((), (work,), home=home, mask_home_credentials=False).text
    real_home = str(home.resolve())
    assert f"{real_home}/.local/state" not in lines  # blocked by the 0700 .local ancestor
    assert f"{real_home}/.netrc" not in lines  # symlink lands behind the 0700 priv dir
    assert f"{real_home}/.npmrc" in lines  # the reachable one is still masked


def test_a_home_below_a_private_ancestor_masks_nothing(world_home):
    # A home that is itself 0755 but sits under a directory the account cannot enter is unreachable,
    # so nothing is masked — binding over its credentials would EPERM and fail the run (and they
    # cannot leak either, being behind the private ancestor).
    outer = world_home / "outer"
    outer.mkdir()
    home = outer / "home"
    home.mkdir()
    work = home / "work"
    work.mkdir()
    (home / ".netrc").write_text("secret\n")
    (home / ".netrc").chmod(0o644)
    outer.chmod(0o700)  # the account cannot traverse into outer to reach the home
    lines = bwrap.BACKEND.policy((), (work,), home=home, mask_home_credentials=False).text
    real_home = str(home.resolve())
    assert f"{real_home}/.netrc" not in lines  # home unreachable from / → masked nothing
    assert str(work.resolve()) in lines  # the write bind is unaffected


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
    monkeypatch.setattr(bwrap.os.path, "isdir", lambda p: p == "/run/user")
    lines = bwrap.BACKEND.policy((), (), home=_home(tmp_path)).text.splitlines()
    i = lines.index("/run/user")
    assert lines[i - 1] == "--tmpfs"


def test_socket_dirs_mask_the_whole_tree_not_one_uid(monkeypatch):
    # Regression (round-3 review): the tree /run/user, not /run/user/<launcher-uid>, so a P4
    # account uid's /run/user/<uid>/bus is covered too. Must not depend on os.getuid().
    monkeypatch.setattr(bwrap.os.path, "isdir", lambda p: p == "/run/user")
    assert bwrap._socket_dirs_to_empty() == ["/run/user"]


def test_policy_always_dies_with_parent():
    # Escaped-process cleanup relies on --die-with-parent together with --new-session: if the
    # verifier dies, bwrap and the namespace go with it. It must always be in the policy.
    lines = bwrap.BACKEND.policy((), (), home=Path("/home/x")).text.splitlines()
    assert "--die-with-parent" in lines


def test_dangerous_sockets_cover_the_root_equivalent_services():
    # The real list must mask the known root-equivalent daemon socket files (#85). libvirt's
    # sockets are covered by the /run/libvirt directory tmpfs instead (see the next test).
    required = {
        "/run/docker.sock",
        "/run/dbus/system_bus_socket",
        "/var/snap/lxd/common/lxd/unix.socket",
        "/var/lib/incus/unix.socket",
        "/run/incus/unix.socket",
    }
    assert required <= set(_REAL_DANGEROUS_SOCKETS)


def test_socket_dirs_cover_runtime_x11_and_libvirt(monkeypatch):
    # /run/user, /tmp/.X11-unix and /run/libvirt (all its daemon sockets) are emptied as whole
    # trees. Patch them all present so the pure function returns the full set it would mask.
    trees = {"/run/user", bwrap._X11_SOCKET_DIR, "/run/libvirt"}
    monkeypatch.setattr(bwrap.os.path, "isdir", lambda p: p in trees)
    assert set(bwrap._socket_dirs_to_empty()) == trees


def test_x11_socket_dir_is_masked_with_tmpfs(tmp_path, monkeypatch):
    # A reachable X server socket (/tmp/.X11-unix/X*) lets a candidate inject input / run commands
    # outside the sandbox on a desktop; the whole dir is emptied with a tmpfs when it exists.
    import os

    monkeypatch.setattr(bwrap.os.path, "isdir", lambda p: p == bwrap._X11_SOCKET_DIR)
    lines = bwrap.BACKEND.policy((), (), home=_home(tmp_path)).text.splitlines()
    real_x11 = os.path.realpath(bwrap._X11_SOCKET_DIR)  # _safe() resolves /tmp on macOS
    i = lines.index(real_x11)
    assert lines[i - 1] == "--tmpfs"


def test_socket_dirs_absent_add_no_tmpfs(tmp_path, monkeypatch):
    monkeypatch.setattr(bwrap.os.path, "isdir", lambda p: False)
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
