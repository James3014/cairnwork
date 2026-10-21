"""The Linux bwrap backend is a P2 draft: the availability probe fails closed when bwrap is
absent, and the not-yet-implemented methods raise rather than letting anything run unconfined.
The real Linux behaviour (under the AppArmor userns restriction) is observed by the
test-linux-sandbox CI job, which cannot run on macOS."""

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


def test_draft_methods_fail_closed():
    with pytest.raises(SandboxError):
        bwrap.BACKEND.policy((), ())
    with pytest.raises(SandboxError):
        bwrap.BACKEND.wrap(["x"], Path("/x"))
    with pytest.raises(SandboxError):
        bwrap.BACKEND.unconfined(["x"])


def test_backend_not_wired_in_yet():
    # The draft deliberately does not return the bwrap backend from platform_backend(), so the
    # core still finds no backend on Linux and fails closed until the next PR wires it in.
    import sys

    if sys.platform.startswith("linux"):
        assert platform_backend() is None
