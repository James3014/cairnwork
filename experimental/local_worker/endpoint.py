"""Loopback-only OpenAI-compatible chat endpoint for the local worker loop (#88, Plan v2 point 3).

Only ``http://127.0.0.1:<port>`` or ``http://[::1]:<port>``: literal addresses (no hostname, so no
resolver or /etc/hosts in the path), an explicit port, no credentials, query or fragment. Requests
go through ``http.client`` directly, which has no proxy layer (``http_proxy`` and friends are never
consulted) and never follows redirects; any non-200 answer is an error. After connecting, the peer
address must itself be loopback. Every request takes the loop's single deadline (monotonic seconds):
the socket timeout is recomputed from it before each step, the body is read with read1 (one receive
per call) and checked against it between reads, and a watchdog timer shuts the socket down at the
deadline itself, so a server that drips headers or body cannot hold a request past it.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

DEFAULT_URL = "http://127.0.0.1:8000/v1"
LOOPBACK_LITERALS = ("127.0.0.1", "::1")
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_PATH = re.compile(r"(/[A-Za-z0-9._~-]+)*")


class EndpointError(RuntimeError):
    """The endpoint answered badly or not at all: the episode aborts (ERROR_ABORT)."""


class EndpointTimeout(EndpointError):
    """The single deadline passed while waiting for the endpoint (TIMEOUT)."""


class Endpoint(Protocol):
    url: str

    def chat(self, payload: dict, deadline: float) -> dict: ...

    def models(self, deadline: float) -> list[str]: ...


@dataclass(frozen=True)
class LoopbackURL:
    host: str
    port: int
    base_path: str

    def __str__(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}{self.base_path}"


def parse_loopback_url(url: str) -> LoopbackURL:
    """The endpoint URL, or ValueError unless it is plain http to a literal loopback address."""
    parts = urlsplit(url)
    if parts.scheme != "http":
        raise ValueError(f"endpoint must be http://, got {parts.scheme!r}")
    if "@" in parts.netloc or parts.query or parts.fragment:
        raise ValueError("endpoint must not carry credentials, a query or a fragment")
    host = parts.hostname
    if host not in LOOPBACK_LITERALS:
        raise ValueError(f"endpoint host must be the literal 127.0.0.1 or [::1], got {host!r}")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"endpoint port is invalid: {exc}") from None
    if port is None or port == 0:
        raise ValueError("endpoint needs an explicit port")
    path = parts.path.rstrip("/")
    if not _PATH.fullmatch(path):
        raise ValueError(f"endpoint path {path!r} has characters outside [A-Za-z0-9._~-/]")
    return LoopbackURL(host, port, path)


def peer_is_loopback(peer: Any) -> bool:
    try:
        return ipaddress.ip_address(str(peer[0]).split("%")[0]).is_loopback
    except (ValueError, IndexError, TypeError):
        return False


Clock = Callable[[], float]


class LoopbackEndpoint:
    def __init__(self, url: str = DEFAULT_URL, clock: Clock = time.monotonic) -> None:
        self.target = parse_loopback_url(url)
        self.url = str(self.target)
        self.clock = clock

    def chat(self, payload: dict, deadline: float) -> dict:
        body = json.dumps(payload).encode()
        return self._request("POST", "/chat/completions", body, deadline)

    def models(self, deadline: float) -> list[str]:
        data = self._request("GET", "/models", None, deadline).get("data")
        if not isinstance(data, list):
            raise EndpointError("/models answered without a data list")
        return [str(m["id"]) for m in data if isinstance(m, dict) and "id" in m]

    def _remaining(self, deadline: float) -> float:
        left = deadline - self.clock()
        if left <= 0:
            raise EndpointTimeout("deadline passed")
        return left

    def _request(self, method: str, path: str, body: bytes | None, deadline: float) -> dict:
        t = self.target
        conn = http.client.HTTPConnection(t.host, t.port, timeout=self._remaining(deadline))
        watchdog: threading.Timer | None = None
        fired = threading.Event()  # set by the watchdog before it shuts the socket down
        try:
            conn.connect()
            sock = conn.sock
            if sock is None or not peer_is_loopback(sock.getpeername()):
                raise EndpointError("connected peer is not a loopback address")
            watchdog = threading.Timer(self._remaining(deadline), _shutdown, (sock, fired))
            watchdog.daemon = True
            watchdog.start()
            headers = {"Content-Type": "application/json"} if body is not None else {}
            conn.request(method, t.base_path + path, body=body, headers=headers)
            sock.settimeout(self._remaining(deadline))
            resp = conn.getresponse()
            data = _read_bounded(resp, sock, lambda: self._remaining(deadline))
            status = resp.status
            if fired.is_set():  # a shut-down socket reads as end of body: never parse that
                raise EndpointTimeout("endpoint passed the deadline")
        except TimeoutError as exc:
            raise EndpointTimeout(f"endpoint timed out: {exc}") from None
        except (OSError, http.client.HTTPException) as exc:
            if fired.is_set() or self.clock() >= deadline:  # the watchdog shut the socket down
                raise EndpointTimeout("endpoint passed the deadline") from None
            raise EndpointError(f"endpoint request failed: {type(exc).__name__}: {exc}") from None
        finally:
            if watchdog is not None:
                watchdog.cancel()
            conn.close()
        if status != 200:  # a redirect is an error too: it is never followed
            raise EndpointError(f"endpoint answered HTTP {status}")
        try:
            parsed = json.loads(data)
        except ValueError:
            raise EndpointError("endpoint answered with invalid JSON") from None
        if not isinstance(parsed, dict):
            raise EndpointError("endpoint answered with a non-object JSON value")
        return parsed


def _read_bounded(resp: http.client.HTTPResponse, sock: Any, remaining: Callable[[], float]):
    chunks: list[bytes] = []
    size = 0
    while not resp.isclosed():  # http.client closes the socket once the body is complete
        sock.settimeout(remaining())
        chunk = resp.read1(65536)  # one receive: read(n) would loop recv until n bytes
        if not chunk:
            if getattr(resp, "length", None):  # Content-Length not reached: a cut-off body
                raise http.client.IncompleteRead(b"".join(chunks), resp.length)
            return b"".join(chunks)
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise EndpointError("endpoint response exceeds the size cap")
        chunks.append(chunk)
    return b"".join(chunks)


def _shutdown(sock: socket.socket, fired: threading.Event) -> None:
    fired.set()
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass  # already closed: the request finished first


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def parse_usage(resp: dict) -> tuple[int | None, int | None, int | None, str]:
    """(prompt, completion, cached, usage_status) from a response; never invents a count.

    usage_status: ``ok`` (all three present), ``no_cache_field`` (prompt and completion present,
    cached not reported: cached is null), ``malformed`` (usage present but prompt or completion
    missing or not a non-negative integer: those are null) or ``missing`` (no usage object).
    """
    usage = resp.get("usage")
    if usage is None:
        return None, None, None, "missing"
    if not isinstance(usage, dict):
        return None, None, None, "malformed"
    prompt = _count(usage.get("prompt_tokens"))
    completion = _count(usage.get("completion_tokens"))
    details = usage.get("prompt_tokens_details")
    cached = _count(details.get("cached_tokens")) if isinstance(details, dict) else None
    if prompt is None or completion is None:
        return prompt, completion, cached, "malformed"
    return prompt, completion, cached, "ok" if cached is not None else "no_cache_field"
