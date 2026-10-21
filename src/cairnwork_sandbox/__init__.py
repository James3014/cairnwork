"""OS sandbox backends for running untrusted candidate code (a trusted package).

The Cairnwork core calls only ``select()`` / ``platform_backend()`` and the ``Backend``
interface (backend.py). This package must not import the core: only the core calls it.
Today the only backend is macOS Seatbelt (seatbelt.py); on any other platform there is
no backend, and callers fail closed (nothing runs unconfined).
"""

from __future__ import annotations

import sys

from cairnwork_sandbox.backend import Backend, Policy, SandboxError

__all__ = ["Backend", "Policy", "SandboxError", "platform_backend", "select"]


def platform_backend() -> Backend | None:
    """This platform's backend, not yet probed (None: no backend for this platform)."""
    if sys.platform == "darwin":
        from cairnwork_sandbox import seatbelt

        return seatbelt.BACKEND
    return None


def select() -> Backend | None:
    """A backend that works here, or None: the caller must then run nothing."""
    backend = platform_backend()
    if backend is None or not backend.available():
        return None
    return backend
