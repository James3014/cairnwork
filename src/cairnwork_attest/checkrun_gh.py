"""Post a Check Run through a ``gh``-compatible CLI instead of a raw token.

The runner can hand ``check-run`` / ``on-verified`` the path of a ``gh`` wrapper that
authenticates by itself (for example a GitHub App helper). The token then never enters
this process: ``gh api`` sends the request and prints the status line, headers and body.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

GhHttp = Callable[[str, str, dict[str, str], bytes], tuple[int, bytes]]
_API_PREFIX = "https://api.github.com/"
_TIMEOUT_SECONDS = 30
_PASSED_HEADERS = ("Accept", "X-GitHub-Api-Version")


def gh_problem(gh: Path) -> str | None:
    """Why ``gh`` cannot be used, or None."""
    if not gh.is_absolute():
        return "--gh must be an absolute path"
    if not gh.is_file() or not os.access(gh, os.X_OK):
        return "--gh is not an executable file"
    return None


def _parse_include(out: bytes) -> tuple[int, bytes]:
    """Split ``gh api --include`` output into (status, body)."""
    for sep in (b"\r\n\r\n", b"\n\n"):
        head, found, body = out.partition(sep)
        if found:
            break
    else:
        raise OSError("gh api printed no headers")
    parts = head.split(b"\n", 1)[0].split()
    if len(parts) < 2 or not parts[0].startswith(b"HTTP/") or not parts[1].isdigit():
        raise OSError("gh api printed no HTTP status line")
    return int(parts[1]), body


def gh_http(gh: Path) -> GhHttp:
    """An Http callable for checkrun.post_check_run that goes through ``gh api``."""

    def call(url: str, method: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        if not url.startswith(_API_PREFIX):
            raise OSError("unexpected API url")
        argv = [str(gh), "api", "--method", method, "--include", url[len(_API_PREFIX) :]]
        for name in _PASSED_HEADERS:
            if name in headers:
                argv += ["-H", f"{name}: {headers[name]}"]
        argv += ["--input", "-"]
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, operator-chosen gh
                argv, input=body, capture_output=True, timeout=_TIMEOUT_SECONDS, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise OSError("gh api timed out") from exc
        return _parse_include(proc.stdout)

    return call
