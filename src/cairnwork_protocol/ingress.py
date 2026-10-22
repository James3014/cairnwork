"""Rules every network entry into Cairnwork must follow (PRESERVED_BEHAVIORS #1).

Bearer token required and compared with ``hmac.compare_digest`` on bytes; loopback-only
bind with no "allow remote" switch; request body at most 1 MiB; error responses carry
a fixed code, never exception text or paths. Nothing in the core listens on a socket
today; a future entry point calls ``serve`` (the only public way to listen: it checks
the bind, sets a per-connection timeout and builds the server) and tests/test_ingress.py
scans the core for http.server / socketserver / listening sockets.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import socket
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BufferedIOBase
from typing import Any

MAX_BODY_BYTES = 1 << 20
MAX_LENGTH_DIGITS = 8
REQUEST_TIMEOUT_S = 10.0

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
    """Exact, case-sensitive ``Bearer `` prefix (stricter than RFC 7235, fail-closed)."""
    if not isinstance(header, str) or not token:
        return False
    return hmac.compare_digest(_bytes(header), b"Bearer " + _bytes(token))


def read_body(headers: Any, rfile: BufferedIOBase, limit: int = MAX_BODY_BYTES) -> bytes:
    # No chunked (or any) transfer coding: we frame by Content-Length only, so a
    # Transfer-Encoding header, alone or beside Content-Length, is request smuggling bait.
    if headers.get("Transfer-Encoding") is not None:
        raise IngressError("INGRESS_TRANSFER_ENCODING_UNSUPPORTED")
    values = headers.get_all("Content-Length") if hasattr(headers, "get_all") else None
    if values is None:
        value = headers.get("Content-Length")
        values = [] if value is None else [value]
    if not values:
        raise IngressError("INGRESS_LENGTH_REQUIRED", 411)
    if len(values) != 1:
        raise IngressError("INGRESS_LENGTH_INVALID")
    text = values[0].strip()
    if not (text.isascii() and text.isdecimal()) or len(text) > MAX_LENGTH_DIGITS:
        raise IngressError("INGRESS_LENGTH_INVALID")
    length = int(text)
    if length > limit:
        raise IngressError("INGRESS_BODY_TOO_LARGE", 413)
    try:
        body = rfile.read(length)
    except TimeoutError:
        raise IngressError("INGRESS_BODY_TIMEOUT", 408) from None
    if len(body) != length:
        raise IngressError("INGRESS_BODY_TRUNCATED")
    return body


_HTTP_CODES = {
    400: "INGRESS_BAD_REQUEST",
    408: "INGRESS_REQUEST_TIMEOUT",
    414: "INGRESS_REQUEST_TOO_LARGE",
    431: "INGRESS_REQUEST_TOO_LARGE",
}


def _host_allowed(header: str | None, port: int) -> bool:
    # The explicit bound port is required on purpose: a bare host would mean port 80,
    # and this service never binds 80, so a bare-host request is not for us.
    if not isinstance(header, str):
        return False
    return header.lower() in {f"{name}:{port}" for name in ("localhost", "127.0.0.1", "[::1]")}


def _make_handler(
    token: str, routes: Mapping[str, Route], timeout: float
) -> type[BaseHTTPRequestHandler]:
    secret = require_token(token)
    table = dict(routes)
    limit = timeout

    class Handler(BaseHTTPRequestHandler):
        timeout = limit  # a stalled client frees its thread instead of holding it forever

        def version_string(self) -> str:
            return "cairnwork"

        def _send(self, status: int, payload: Mapping[str, Any], allow: str | None = None) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if allow:
                self.send_header("Allow", allow)
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def send_error(self, code: int, message: str | None = None, explain: str | None = None):
            # Stock pages are HTML and echo request text; every error is a fixed JSON code.
            self.close_connection = True
            if code in (405, 501):
                self._send(405, {"error": "INGRESS_METHOD_NOT_ALLOWED"}, allow="POST")
            else:
                self._send(code, {"error": _HTTP_CODES.get(code, "INGRESS_HTTP_ERROR")})

        def do_POST(self) -> None:
            try:
                if len(self.headers.get_all("Host", [])) > 1:
                    raise IngressError("INGRESS_HOST_DUPLICATE")
                if not _host_allowed(self.headers.get("Host"), self.server.server_address[1]):
                    raise IngressError("INGRESS_HOST_NOT_ALLOWED", 421)
                if len(self.headers.get_all("Authorization", [])) > 1 or not authorized(
                    self.headers.get("Authorization"), secret
                ):
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


def serve(
    host: str,
    port: int,
    token: str,
    routes: Mapping[str, Route],
    *,
    timeout: float = REQUEST_TIMEOUT_S,
) -> ThreadingHTTPServer:
    """Build (not start) a loopback-only server; refuses any non-loopback ``host``."""
    check_bind(host, port)
    handler = _make_handler(token, routes, timeout)

    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
        daemon_threads = True

    return Server((host, port), handler)
