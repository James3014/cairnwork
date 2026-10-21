"""Calling a Nexus Core HTTP service: ``POST /v1/changesets/verify``.

Same contract as the in-process call (request dict in, ``(HTTP status, response dict)``
out), so the notary cannot tell them apart. Plain http is accepted only to a loopback
host (Core's own server binds loopback only); anything else must be https. The bearer
token is read from a file the owner controls, at call time, and never logged; requests
ignore proxy settings and never follow redirects.
"""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

VERIFY_PATH = "/v1/changesets/verify"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class CoreHttpError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        raise CoreHttpError(f"Core answered a redirect ({code}); not followed")


# No proxy (environment proxies would see the bearer token) and no redirects (they
# would carry it to a host the URL check never approved).
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_core_url(base_url: str) -> str:
    if not isinstance(base_url, str):
        raise CoreHttpError("core_url must be a string")
    try:
        parts = urlsplit(base_url)
        parts.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError as exc:
        raise CoreHttpError(f"core_url is malformed: {exc}") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise CoreHttpError(f"core_url must be an http(s) URL: {base_url!r}")
    if parts.scheme == "http" and not _loopback(parts.hostname):
        raise CoreHttpError("plain http is allowed only to a loopback Core")
    if parts.path not in ("", "/") or parts.query or parts.fragment or "@" in parts.netloc:
        raise CoreHttpError("core_url is the service root only (scheme://host:port)")
    return f"{parts.scheme}://{parts.netloc}"


def http_core_verify(
    base_url: str, token_file: Path, timeout: float = 60.0
) -> Callable[[dict[str, Any]], tuple[int, dict[str, Any]]]:
    root = check_core_url(base_url)

    def verify(request: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        token = Path(token_file).read_text(encoding="utf-8").strip()
        if not token:
            raise CoreHttpError("empty Core token")
        call = urllib.request.Request(  # noqa: S310 - scheme checked in check_core_url
            root + VERIFY_PATH,
            data=json.dumps(request).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        )
        try:
            with _OPENER.open(call, timeout=timeout) as response:
                status, body = response.status, response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:  # a Core refusal still carries its JSON body
            if 300 <= error.code < 400:
                raise CoreHttpError(f"Core answered a redirect ({error.code})") from error
            status, body = error.code, error.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise CoreHttpError("Core response too large")
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise CoreHttpError("Core response is not a JSON object")
        return status, payload

    return verify
