"""PRESERVED #1: ingress authentication, loopback-only bind, 1 MiB body limit."""

from __future__ import annotations

import ast
import hmac
import io
import json
import socket
import threading
import time
from email.message import Message
from http.client import HTTPConnection
from pathlib import Path

import pytest

from cairnwork_protocol import ingress
from cairnwork_protocol.ingress import (
    IngressError,
    authorized,
    check_bind,
    read_body,
    require_token,
    serve,
)

TOKEN = "s3cret-token"
CORE = Path(__file__).resolve().parents[1] / "src" / "cairnwork"


def _echo(body: bytes):
    return 200, {"size": len(body)}


@pytest.fixture
def server():
    def boom(_body):
        raise RuntimeError("/secret/path exploded")

    httpd = serve("127.0.0.1", 0, TOKEN, {"/echo": _echo, "/boom": boom}, timeout=0.5)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()
    thread.join()


def _post(
    port: int,
    path: str = "/echo",
    body: bytes | None = b"{}",
    headers: dict[str, str] | None = None,
    raw_length: str | None = None,
):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    sent = headers if headers is not None else {"Authorization": f"Bearer {TOKEN}"}
    conn.putrequest("POST", path, skip_host="Host" in sent)
    for key, value in sent.items():
        conn.putheader(key, value)
    if raw_length is not None:
        conn.putheader("Content-Length", raw_length)
    elif raw_length is None and body is not None:
        conn.putheader("Content-Length", str(len(body)))
    conn.endheaders()
    if body and raw_length in (None, str(len(body))):
        conn.send(body)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


def test_correct_token_gets_200(server):
    assert _post(server)[0] == 200


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": TOKEN},
        {"Authorization": "Bearer caf\xe9"},
    ],
)
def test_bad_credentials_are_401_without_an_exception(server, headers):
    status, data = _post(server, headers=headers)
    assert status == 401
    assert json.loads(data) == {"error": "INGRESS_UNAUTHORIZED"}


def test_non_ascii_header_never_raises_type_error():
    assert authorized("Bearer caf\xe9", TOKEN) is False
    assert authorized("Bearer \ud800", TOKEN) is False
    assert authorized(None, TOKEN) is False
    assert authorized(f"Bearer {TOKEN}", "") is False
    assert authorized(f"Bearer {TOKEN}", TOKEN) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "example.com", "192.168.1.5", ""])
def test_check_bind_refuses_non_loopback(host):
    with pytest.raises(IngressError) as err:
        check_bind(host, 8080)
    assert err.value.code == "INGRESS_BIND_NOT_LOOPBACK"


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_check_bind_accepts_loopback(host):
    check_bind(host, 8080)


@pytest.mark.parametrize("port", [-1, 65536, "80", None, True])
def test_check_bind_rejects_bad_ports(port):
    with pytest.raises(IngressError) as err:
        check_bind("127.0.0.1", port)
    assert err.value.code == "INGRESS_BIND_PORT_INVALID"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_empty_token_fails_at_startup(value):
    with pytest.raises(IngressError) as err:
        require_token(value)
    assert err.value.code == "INGRESS_TOKEN_REQUIRED"
    with pytest.raises(IngressError):
        serve("127.0.0.1", 0, value, {})


def _headers(*lengths):
    msg = Message()
    for length in lengths:
        msg["Content-Length"] = length
    return msg


@pytest.mark.parametrize(
    ("lengths", "code", "status"),
    [
        ((), "INGRESS_LENGTH_REQUIRED", 411),
        (("abc",), "INGRESS_LENGTH_INVALID", 400),
        (("-1",), "INGRESS_LENGTH_INVALID", 400),
        (("١",), "INGRESS_LENGTH_INVALID", 400),
        (("1", "1"), "INGRESS_LENGTH_INVALID", 400),
        (("1" * 5000,), "INGRESS_LENGTH_INVALID", 400),
        (("123456789",), "INGRESS_LENGTH_INVALID", 400),
        ((str((1 << 20) + 1),), "INGRESS_BODY_TOO_LARGE", 413),
    ],
)
def test_content_length_rules(lengths, code, status):
    with pytest.raises(IngressError) as err:
        read_body(_headers(*lengths), io.BytesIO(b"x" * 10))
    assert (err.value.code, err.value.status) == (code, status)


def test_exactly_one_mebibyte_passes_and_truncation_fails():
    body = b"x" * (1 << 20)
    assert read_body(_headers(str(1 << 20)), io.BytesIO(body)) == body
    with pytest.raises(IngressError) as err:
        read_body(_headers("10"), io.BytesIO(b"abc"))
    assert err.value.code == "INGRESS_BODY_TRUNCATED"


def test_http_length_statuses(server):
    assert _post(server, body=None)[0] == 411
    assert _post(server, body=b"", raw_length="abc")[0] == 400
    assert _post(server, body=b"", raw_length="-1")[0] == 400
    assert _post(server, body=b"", raw_length=str((1 << 20) + 1))[0] == 413
    assert _post(server, body=b"x" * (1 << 20)) == (200, b'{"size": 1048576}')


def test_auth_is_checked_before_the_body_is_read(server):
    status, _ = _post(server, body=b"", raw_length=str((1 << 20) + 1), headers={})
    assert status == 401


def test_errors_carry_no_exception_text_or_paths(server):
    status, data = _post(server, "/boom")
    assert status == 500
    assert json.loads(data) == {"error": "INGRESS_INTERNAL"}
    assert b"secret" not in data and b"exploded" not in data
    assert _post(server, "/missing")[0] == 404


def test_token_comparison_uses_compare_digest(monkeypatch):
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((type(a), type(b)))
        return real(a, b)

    monkeypatch.setattr(ingress.hmac, "compare_digest", spy)
    assert authorized(f"Bearer {TOKEN}", TOKEN)
    assert not authorized("Bearer nope", TOKEN)
    assert calls == [(bytes, bytes)] * 2


_LISTEN_ATTRS = {"listen", "serve_forever", "create_server", "start_server"}
_LISTEN_MODULES = {"http.server", "socketserver", "wsgiref.simple_server", "xmlrpc.server"}


def _listens(path: Path) -> list[str]:
    found = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        found += [
            n
            for n in names
            if n.split(".")[0] in {"http", "socketserver", "wsgiref", "xmlrpc"}
            and n not in {"http.client", "xmlrpc.client"}
        ]
        if isinstance(node, ast.ImportFrom) and node.module in {"http", "wsgiref", "xmlrpc"}:
            found += [a.name for a in node.names if a.name in {"server", "simple_server"}]
        if isinstance(node, ast.Attribute) and node.attr in _LISTEN_ATTRS:
            found.append(node.attr)
        if (  # socket.bind((host, port)) / bind("/path"); ledger.bind(key, ...) is not a socket
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "bind"
            and node.args
            and isinstance(node.args[0], ast.Tuple | ast.Constant)
        ):
            found.append("bind")
        if isinstance(node, ast.Name) and node.id in _LISTEN_ATTRS:
            found.append(node.id)
        if isinstance(node, ast.Constant) and node.value in _LISTEN_MODULES:
            found.append(node.value)  # importlib.import_module("http.server") and friends
    return found


def test_core_has_no_hidden_network_entry():
    for path in sorted(CORE.rglob("*.py")):
        assert _listens(path) == [], path


def test_guard_scan_catches_a_planted_listener(tmp_path):
    for source in (
        "import socketserver",
        "from http.server import HTTPServer",
        "from http import server",
        "s.listen(1)",
        "import socket\nsocket.create_server(('', 1))",
        "import asyncio\nasyncio.start_server(f, '', 1)",
        "import wsgiref.simple_server",
        "from wsgiref.simple_server import make_server",
        "import xmlrpc.server",
        "import importlib\nimportlib.import_module('http.server')",
        "s = x()\ns.bind(('', 1))",
    ):
        planted = tmp_path / "planted.py"
        planted.write_text(source + "\n", encoding="utf-8")
        assert _listens(planted), source


def _raw(port: int, payload: bytes, wait: float = 3.0) -> bytes:
    sock = socket.create_connection(("127.0.0.1", port), timeout=wait)
    sock.sendall(payload)
    start, data = time.monotonic(), b""
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:  # peer closed
                break
            data += chunk
    finally:
        sock.close()
    assert time.monotonic() - start < wait
    return data


def test_stalled_headers_are_closed(server):
    _raw(server, b"POST /echo HTTP/1.1\r\nHost: 127.0.0.1\r\n")


def test_short_body_is_closed(server):
    data = _raw(
        server,
        f"POST /echo HTTP/1.1\r\nHost: 127.0.0.1:{server}\r\n"
        f"Authorization: Bearer {TOKEN}\r\nContent-Length: 100\r\n\r\nab".encode(),
    )
    assert b"408" in data.split(b"\r\n", 1)[0]


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "example.com", "192.168.1.5"])
def test_serve_refuses_non_loopback(host):
    with pytest.raises(IngressError) as err:
        serve(host, 0, TOKEN, {})
    assert err.value.code == "INGRESS_BIND_NOT_LOOPBACK"


def test_serve_binds_loopback_only(server):
    assert _post(server)[0] == 200


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "OPTIONS", "PATCH", "FOO"])
def test_other_methods_get_json_405_and_neutral_server_header(server, method):
    conn = HTTPConnection("127.0.0.1", server, timeout=5)
    conn.request(method, "/echo")
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    assert resp.status == 405
    assert json.loads(data) == {"error": "INGRESS_METHOD_NOT_ALLOWED"}
    assert resp.getheader("Content-Type") == "application/json"
    assert resp.getheader("Server") == "cairnwork"
    assert "Python" not in (resp.getheader("Server") or "")


def test_malformed_request_line_is_json(server):
    data = _raw(server, b"POST\r\n\r\n")
    assert data.endswith(b'{"error": "INGRESS_BAD_REQUEST"}') or b"INGRESS_" in data
    assert b"<html" not in data.lower()


def test_duplicate_authorization_is_refused(server):
    conn = HTTPConnection("127.0.0.1", server, timeout=5)
    conn.putrequest("POST", "/echo")
    conn.putheader("Authorization", f"Bearer {TOKEN}")
    conn.putheader("Authorization", f"Bearer {TOKEN}")
    conn.putheader("Content-Length", "2")
    conn.endheaders(b"{}")
    resp = conn.getresponse()
    assert resp.status == 401
    conn.close()


def test_bearer_prefix_is_case_sensitive():
    assert authorized(f"bearer {TOKEN}", TOKEN) is False


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1:1", "127.0.0.1", "localhost"])
def test_host_header_allow_list(server, host):
    status, data = _post(server, headers={"Authorization": f"Bearer {TOKEN}", "Host": host})
    assert status == 421
    assert json.loads(data) == {"error": "INGRESS_HOST_NOT_ALLOWED"}


@pytest.mark.parametrize("name", ["localhost", "127.0.0.1", "[::1]", "LOCALHOST"])
def test_host_header_allowed_names(server, name):
    headers = {"Authorization": f"Bearer {TOKEN}", "Host": f"{name}:{server}"}
    assert _post(server, headers=headers)[0] == 200
