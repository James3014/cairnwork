"""Rules every network entry into Cairnwork must follow (PRESERVED_BEHAVIORS #1).

Bearer token required and compared with ``hmac.compare_digest`` on bytes; loopback-only
bind with no "allow remote" switch; request body at most 1 MiB; error responses carry
a fixed code, never exception text or paths. Nothing in the core listens on a socket
today; a future entry point builds its handler here (tests/test_ingress.py scans the
core for ``http.server`` / ``socketserver`` / listening sockets).
"""

from __future__ import annotations

import hmac
import ipaddress
import json
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler
from io import BufferedIOBase
from typing import Any

MAX_BODY_BYTES = 1 << 20

Route = Callable[[bytes], tuple[int, Mapping[str, Any]]]


class IngressError(Exception):
    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def check_bind(host: str, port: int) -> None:
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise IngressError("INGRESS_BIND_PORT_INVALID")
    if host == "localhost":
        return
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise IngressError("INGRESS_BIND_NOT_LOOPBACK")


def require_token(value: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IngressError("INGRESS_TOKEN_REQUIRED")
    return value


def _bytes(text: str) -> bytes:
    # http.server decodes header bytes as latin-1; never raise on whatever arrives.
    return text.encode("utf-8", "surrogatepass")


def authorized(header: str | None, token: str) -> bool:
    if not isinstance(header, str) or not token:
        return False
    return hmac.compare_digest(_bytes(header), b"Bearer " + _bytes(token))


def read_body(headers: Any, rfile: BufferedIOBase, limit: int = MAX_BODY_BYTES) -> bytes:
    values = headers.get_all("Content-Length") if hasattr(headers, "get_all") else None
    if values is None:
        value = headers.get("Content-Length")
        values = [] if value is None else [value]
    if not values:
        raise IngressError("INGRESS_LENGTH_REQUIRED", 411)
    if len(values) != 1:
        raise IngressError("INGRESS_LENGTH_INVALID")
    text = values[0].strip()
    if not (text.isascii() and text.isdecimal()):
        raise IngressError("INGRESS_LENGTH_INVALID")
    length = int(text)
    if length > limit:
        raise IngressError("INGRESS_BODY_TOO_LARGE", 413)
    body = rfile.read(length)
    if len(body) != length:
        raise IngressError("INGRESS_BODY_TRUNCATED")
    return body


def make_handler(token: str, routes: Mapping[str, Route]) -> type[BaseHTTPRequestHandler]:
    secret = require_token(token)
    table = dict(routes)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: Mapping[str, Any]) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            try:
                if not authorized(self.headers.get("Authorization"), secret):
                    raise IngressError("INGRESS_UNAUTHORIZED", 401)
                route = table.get(self.path)
                if route is None:
                    raise IngressError("INGRESS_NOT_FOUND", 404)
                status, payload = route(read_body(self.headers, self.rfile))
            except IngressError as exc:
                self.close_connection = True  # an unread body would poison keep-alive
                self._send(exc.status, {"error": exc.code})
                return
            except Exception:  # noqa: BLE001 - fixed answer, no exception text
                self.close_connection = True
                self._send(500, {"error": "INGRESS_INTERNAL"})
                return
            self._send(status, payload)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    return Handler
