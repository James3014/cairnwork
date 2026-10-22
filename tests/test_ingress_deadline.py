"""Whole-request deadline and quiet early disconnect for the ingress (Issue #147)."""

from __future__ import annotations

import errno
import json
import socket
import threading
import time
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler

import pytest

from cairnwork_protocol.ingress import IngressError, serve

TOKEN = "s3cret-token"


@pytest.fixture
def server():
    httpd = serve("127.0.0.1", 0, TOKEN, {"/echo": lambda b: (200, {"size": len(b)})}, timeout=0.5)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()
    thread.join()


def _trickle(port: int, payload: bytes, gap: float, budget: float) -> float:
    """Send one byte per ``gap`` seconds; return how long until the server cut us off."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    start = time.monotonic()
    try:
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
    finally:
        sock.close()


def test_trickling_headers_are_cut_at_the_whole_request_deadline(server):
    # timeout=0.5; a byte every 0.2 s never trips a per-read timeout.
    head = (
        f"POST /echo HTTP/1.1\r\nHost: 127.0.0.1:{server}\r\n"
        f"Authorization: Bearer {TOKEN}\r\nX-Pad: {'a' * 200}\r\n\r\n"
    ).encode()
    assert _trickle(server, head, gap=0.2, budget=6.0) < 2.0


def test_trickling_body_is_cut_at_the_whole_request_deadline(server):
    head = (
        f"POST /echo HTTP/1.1\r\nHost: 127.0.0.1:{server}\r\n"
        f"Authorization: Bearer {TOKEN}\r\nContent-Length: 200\r\n\r\n"
    ).encode()
    deadline = 0.5
    sock = socket.create_connection(("127.0.0.1", server), timeout=5)
    start = time.monotonic()
    data = b""
    try:
        try:
            sock.sendall(head)
            for _ in range(100):
                sock.sendall(b"x")
                time.sleep(0.2)
                if time.monotonic() - start > 6:
                    break
        except OSError:
            pass  # the server already cut us off (possibly with a reset)
        sock.settimeout(1)
        try:
            data = sock.recv(4096)
        except OSError:
            pass  # a reset instead of the 408 is also a cut
    finally:
        sock.close()
    assert time.monotonic() - start < deadline + 1.5
    if data:
        assert b"408" in data.split(b"\r\n", 1)[0]


def test_response_write_gets_the_normal_timeout_back(server, monkeypatch):
    seen: list[float | None] = []
    real = BaseHTTPRequestHandler.end_headers

    def spy(self):
        seen.append(self.connection.gettimeout())
        real(self)

    monkeypatch.setattr(BaseHTTPRequestHandler, "end_headers", spy)
    sock = socket.create_connection(("127.0.0.1", server), timeout=5)
    try:
        sock.sendall(
            f"POST /echo HTTP/1.1\r\nHost: 127.0.0.1:{server}\r\n"
            f"Authorization: Bearer {TOKEN}\r\nContent-Length: 2\r\n\r\nh".encode()
        )
        time.sleep(0.45)  # the last byte lands ~0.05 s before the 0.5 s deadline
        sock.sendall(b"i")
        reply = sock.recv(4096)
    finally:
        sock.close()
    assert reply.startswith(b"HTTP/1.0 200") or reply.startswith(b"HTTP/1.1 200")
    assert seen == [0.5]


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


def test_each_keepalive_request_gets_a_fresh_deadline(server):
    conn = HTTPConnection("127.0.0.1", server, timeout=5)
    try:
        for _ in range(3):
            conn.request(
                "POST",
                "/echo",
                body=b"hi",
                headers={"Authorization": f"Bearer {TOKEN}", "Host": f"127.0.0.1:{server}"},
            )
            assert conn.getresponse().read()
            time.sleep(0.3)  # total > 0.5 s across requests, each one well inside its own
    finally:
        conn.close()


def test_early_disconnect_logs_one_line_and_no_traceback(capfd, caplog):
    def slow(_body):
        time.sleep(0.4)
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
            time.sleep(0.1)
            # RST on close, so the server's write fails instead of being buffered.
            sock.setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00"
            )
            sock.close()
            time.sleep(1.0)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()
    assert "Traceback" not in capfd.readouterr().err
    assert [r.getMessage() for r in caplog.records].count("ingress: client disconnected early") == 1
