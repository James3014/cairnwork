"""Seatbelt policy construction and probe (macOS backend specifics).

The behavioural witnesses live in test_sandbox.py and run on every backend; these check the
SBPL profile builder and the developer-directory logic. Only the real-binary test needs macOS.
"""

import sys
from pathlib import Path

import pytest

from cairnwork_sandbox import seatbelt

mac_only = pytest.mark.skipif(sys.platform != "darwin", reason="needs macOS sandbox-exec")


@pytest.mark.parametrize(
    "bad", ['/tmp/a"b', "/tmp/a(b", "/tmp/a\\\\b", "/tmp/a\nb", "/tmp/é", "/tmp/a\n"]
)
def test_profile_paths_cannot_rewrite_the_policy(bad):
    with pytest.raises(seatbelt.SandboxError):
        seatbelt.sbpl_path(bad)


def test_profile_resolves_symlinked_paths(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    profile = seatbelt.build_profile((), (tmp_path / "link",), home=tmp_path)
    assert str(real.resolve()) in profile.text
    assert "(deny network*)" in profile.text and "(deny default)" in profile.text


@pytest.mark.parametrize("root", ["/", "/Users", "/private/tmp", "HOME", "HOME_PARENT"])
def test_broad_roots_are_refused(tmp_path, root):
    home = tmp_path / "home"
    home.mkdir()
    path = {"HOME": home, "HOME_PARENT": tmp_path}.get(root, root)
    with pytest.raises(seatbelt.SandboxError):
        seatbelt.build_profile((Path(path),), (), home=home)


@mac_only
@pytest.mark.sandbox
def test_trailing_credential_deny_wins_inside_an_allowed_root(tmp_path):
    """SBPL: the last matching rule wins. A credential path under an allowed read root
    must stay unreadable (a reviewer claimed first-match-wins; this pins the behaviour)."""
    import subprocess

    home = tmp_path / "home"
    keychains = home / "Library" / "Keychains"
    keychains.mkdir(parents=True)
    (keychains / "login.db").write_text("KEY\n")
    (home / "Library" / "ok.txt").write_text("OK\n")
    profile = seatbelt.build_profile((home / "Library",), (), home=home)
    path = tmp_path / "p.sb"
    path.write_text(profile.text)
    run = lambda target: subprocess.run(  # noqa: E731
        seatbelt.wrap(["/bin/cat", str(target)], path), capture_output=True, text=True
    )
    assert run(home / "Library" / "ok.txt").stdout == "OK\n"
    denied = run(keychains / "login.db")
    assert denied.returncode != 0 and "KEY" not in denied.stdout


def test_probe_requires_a_real_denial(monkeypatch):
    seatbelt.available.cache_clear()
    monkeypatch.setattr(seatbelt, "_probe", lambda policy: 0)
    try:
        assert seatbelt.available() is False
    finally:
        seatbelt.available.cache_clear()


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        ("/Library/Developer/CommandLineTools", True),
        ("/Applications/Xcode.app/Contents/Developer", True),
        ("/Applications/Xcode_16.4.app/Contents/Developer", True),
        ("/Library/Developer", False),
        ("/Library/Developer/CoreSimulator", False),
        ("/Applications/XcodeEvil", False),
        ("/Applications/Xcode.app/Contents", False),
        ("/tmp", False),
    ],
)
def test_only_standard_developer_dirs_are_readable(path, allowed):
    assert bool(seatbelt._DEVELOPER_DIR.fullmatch(path)) is allowed


@pytest.mark.parametrize(
    ("selected", "readable"),
    [
        ("/Library/Developer/CommandLineTools", ("/Library/Developer/CommandLineTools",)),
        ("/Applications/Xcode_26.6.app/Contents/Developer", ("/Applications/Xcode_26.6.app",)),
        ("/Applications/Xcode.app/Contents", ()),
    ],
)
def test_developer_dirs_maps_xcode_to_its_bundle(monkeypatch, selected, readable):
    import subprocess

    done = subprocess.CompletedProcess([], 0, stdout=selected + "\n", stderr="")
    monkeypatch.setattr(seatbelt.subprocess, "run", lambda *a, **k: done)
    monkeypatch.setattr(seatbelt.os.path, "realpath", lambda p: p)
    monkeypatch.setattr(seatbelt.os.path, "isdir", lambda p: True)
    seatbelt.developer_dirs.cache_clear()
    try:
        assert seatbelt.developer_dirs() == readable
    finally:
        seatbelt.developer_dirs.cache_clear()


@pytest.mark.parametrize(
    ("dirs", "licensed"),
    [(("/Applications/Xcode.app",), True), (("/Library/Developer/CommandLineTools",), False)],
)
def test_xcode_license_plist_only_with_a_full_xcode(monkeypatch, tmp_path, dirs, licensed):
    monkeypatch.setattr(seatbelt, "developer_dirs", lambda: dirs)
    profile = seatbelt.build_profile((), (tmp_path / "w",), home=tmp_path / "home")
    assert (seatbelt.XCODE_LICENSE_PLIST in profile.text) is licensed
