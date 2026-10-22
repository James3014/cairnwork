"""Whole-request deadline and quiet early disconnect for the ingress (Issue #147)."""

from __future__ import annotations

import errno
import json
import socket
import sys
import threading
import time
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler

import pytest

from cairnwork_protocol import ingress
from cairnwork_protocol.ingress import IngressError, serve

TOKEN = "s3cret-token"


class _Clock:
    """Stand-in for the ``time`` module inside ingress: only ``monotonic``, set by the test."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


def _post(port: int, body: bytes = b"hi") -> bytes:
    return (
        f"POST /echo HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        f"Authorization: Bearer {TOKEN}\r\nContent-Length: {len(body)}\r\n\r\n"
    ).encode() + body


def _trickle(sock: socket.socket, payload: bytes, gap: float, budget: float) -> float:
    """Send one byte per ``gap`` seconds; return how long until the server cut us off."""
    start = time.monotonic()
    for i in range(len(payload)):
        if time.monotonic() - start > budget:
            break
        try:
            sock.sendall(payload[i : i + 1])
        except OSError:
            break
        time.sleep(gap)
    sock.settimeout(max(0.1, budget - (time.monotonic() - start)))
    try:
        while sock.recv(4096):
            pass
    except OSError:
        pass
    return time.monotonic() - start


# The trickle tests use a deadline (1 s) far above the gap (0.1 s) so no per-read timeout can
# trip, and a payload that would take 20+ s to send: only the whole-request deadline can cut it,
# and the pass bar (10 s) leaves a slow runner 9 s of slack.
@pytest.fixture
def slow_server():
    httpd = serve("127.0.0.1", 0, TOKEN, {"/echo": lambda b: (200, {"size": len(b)})}, timeout=1.0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()
    thread.join()


def test_trickling_headers_are_cut_at_the_whole_request_deadline(slow_server):
    head = (
        f"POST /echo HTTP/1.1\r\nHost: 127.0.0.1:{slow_server}\r\n"
        f"Authorization: Bearer {TOKEN}\r\nX-Pad: {'a' * 300}\r\n\r\n"
    ).encode()
    sock = socket.create_connection(("127.0.0.1", slow_server), timeout=5)
    try:
        assert _trickle(sock, head, gap=0.1, budget=20.0) < 10.0
    finally:
        sock.close()


def test_trickling_body_is_cut_at_the_whole_request_deadline(slow_server):
    head = (
        f"POST /echo HTTP/1.1\r\nHost: 127.0.0.1:{slow_server}\r\n"
        f"Authorization: Bearer {TOKEN}\r\nContent-Length: 300\r\n\r\n"
    ).encode()
    sock = socket.create_connection(("127.0.0.1", slow_server), timeout=5)
    data = b""
    try:
        sock.sendall(head)
        elapsed = _trickle(sock, b"x" * 300, gap=0.1, budget=20.0)
        sock.settimeout(1)
        try:
            data = sock.recv(4096)
        except OSError:
            pass  # a reset instead of the 408 is also a cut
    finally:
        sock.close()
    assert elapsed < 10.0
    if data:
        assert b"408" in data.split(b"\r\n", 1)[0]


def test_response_write_gets_the_normal_timeout_back(monkeypatch):
    # Fake clock: the deadline is set at t=0 (5 s), every later read sees t=2, so the reader
    # leaves the socket timeout at 3.0; the response must see the normal 5.0 again.
    # No real time passes in the assertion, so a slow runner cannot change the outcome.
    clock = _Clock()
    calls = []

    def monotonic() -> float:
        calls.append(None)
        clock.now = 0.0 if len(calls) == 1 else 2.0
        return clock.now

    clock.monotonic = monotonic  # type: ignore[method-assign]
    monkeypatch.setattr(ingress, "time", clock)
    seen: list[float | None] = []
    real = BaseHTTPRequestHandler.end_headers

    def spy(self):
        seen.append(self.connection.gettimeout())
        real(self)

    monkeypatch.setattr(BaseHTTPRequestHandler, "end_headers", spy)
    httpd = serve("127.0.0.1", 0, TOKEN, {"/echo": lambda b: (200, {"size": len(b)})}, timeout=5.0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        try:
            sock.sendall(_post(port))
            reply = sock.recv(4096)
        finally:
            sock.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()
    assert reply.startswith(b"HTTP/1.0 200") or reply.startswith(b"HTTP/1.1 200")
    assert seen == [5.0]


def test_non_disconnect_oserror_from_a_route_is_an_internal_error():
    def boom(_body):
        raise OSError(errno.EIO, "disk")

    httpd = serve("127.0.0.1", 0, TOKEN, {"/boom": boom}, timeout=5)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST",
            "/boom",
            body=b"",
            headers={"Authorization": f"Bearer {TOKEN}", "Host": f"127.0.0.1:{port}"},
        )
        resp = conn.getresponse()
        assert resp.status == 500
        assert json.loads(resp.read()) == {"error": "INGRESS_INTERNAL"}
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


@pytest.mark.parametrize("bad", [None, 0, -1, 0.0, float("nan"), float("inf"), True])
def test_serve_rejects_a_non_positive_or_missing_timeout(bad):
    with pytest.raises(IngressError) as exc:
        serve("127.0.0.1", 0, TOKEN, {}, timeout=bad)
    assert exc.value.code == "INGRESS_TIMEOUT_INVALID"


def _read_response(sock: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        assert chunk, "server closed the connection"
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    length = int(
        next(
            h.split(b":")[1] for h in head.split(b"\r\n") if h.lower().startswith(b"content-length")
        )
    )
    while len(rest) < length:
        rest += sock.recv(4096)
    return head


def test_each_keepalive_request_gets_a_fresh_deadline(monkeypatch):
    # The shipped server answers HTTP/1.0 (one request per connection), so run the handler as
    # HTTP/1.1 here and send three requests down ONE raw socket. The fake clock moves 3 s between
    # them against a 5 s deadline: a deadline set once per connection would expire by request 3.
    # The route moves the clock, so it is already 3 s later when the server starts waiting for
    # the next request (a clock moved only by the client would race the server's blocked recv).
    monkeypatch.setattr(BaseHTTPRequestHandler, "protocol_version", "HTTP/1.1")
    clock = _Clock()
    monkeypatch.setattr(ingress, "time", clock)

    def echo(body):
        clock.now += 3.0
        return 200, {"size": len(body)}

    httpd = serve("127.0.0.1", 0, TOKEN, {"/echo": echo}, timeout=5.0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    request = (
        f"POST /echo HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        f"Authorization: Bearer {TOKEN}\r\nContent-Length: 2\r\n\r\nhi"
    ).encode()
    try:
        for _ in range(3):
            sock.sendall(request)
            assert _read_response(sock).startswith(b"HTTP/1.1 200")
    finally:
        sock.close()
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def test_early_disconnect_logs_one_line_and_no_traceback(capfd, caplog):
    entered = threading.Event()
    release = threading.Event()

    def slow(_body):
        entered.set()
        release.wait(10)
        return 200, {"ok": True}

    httpd = serve("127.0.0.1", 0, TOKEN, {"/slow": slow}, timeout=5)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with caplog.at_level("INFO", logger="cairnwork.ingress"):
            sock = socket.create_connection(("127.0.0.1", port), timeout=5)
            sock.sendall(
                f"POST /slow HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                f"Authorization: Bearer {TOKEN}\r\nContent-Length: 0\r\n\r\n".encode()
            )
            assert entered.wait(10)  # the route is running, whatever the runner's speed
            # RST on close, so the server's write fails instead of being buffered.
            sock.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00"
            )
            sock.close()
            release.set()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not caplog.records:
                time.sleep(0.05)
            time.sleep(0.2)  # room for a (wrong) second line
    finally:
        release.set()
        httpd.shutdown()
        httpd.server_close()
        thread.join()
    assert "Traceback" not in capfd.readouterr().err
    assert [r.getMessage() for r in caplog.records].count("ingress: client disconnected early") == 1


def test_non_disconnect_oserror_outside_the_route_reaches_handle_error(monkeypatch):
    # Raised before any route runs (so do_POST's catch-all cannot answer 500 first):
    # only a broken-pipe / reset is "client disconnected early"; EIO must stay loud.
    def boom(self):
        raise OSError(errno.EIO, "disk")

    monkeypatch.setattr(BaseHTTPRequestHandler, "parse_request", boom)
    httpd = serve("127.0.0.1", 0, TOKEN, {"/x": lambda _b: (200, {})}, timeout=5)
    errors: list[BaseException | None] = []
    seen = threading.Event()

    def record(request, client_address):
        errors.append(sys.exc_info()[1])
        seen.set()

    monkeypatch.setattr(httpd, "handle_error", record)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        assert seen.wait(10)
        sock.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()
    assert len(errors) == 1
    assert isinstance(errors[0], OSError) and errors[0].errno == errno.EIO
