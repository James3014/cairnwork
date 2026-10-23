"""The code the canary makes the worker run (through run_tests): it tries every forbidden read,
write and connection itself and prints one ``CANARY_PROBE <json>`` line per attempt. It never
fails, so a pytest exit code other than 0 means the probe could not run at all."""

from __future__ import annotations

import json

MARK = "CANARY_PROBE "

NET_TARGETS = {
    "internet_tcp_1.1.1.1:443": ("tcp", "1.1.1.1", 443),
    "loopback_splash_127.0.0.1:8000": ("tcp", "127.0.0.1", 8000),
    "loopback_gateway_127.0.0.1:11434": ("tcp", "127.0.0.1", 11434),
    "udp_8.8.8.8:53": ("udp", "8.8.8.8", 53),
    "dns_github.com": ("dns", "github.com", 443),
}

_BODY = r"""
import errno
import json
import os
import pwd
import socket

MARK = __MARK__
READS = __READS__
WRITES = __WRITES__
NETS = __NETS__
DENIED = (errno.EPERM, errno.EACCES)


def emit(**row):
    print(MARK + json.dumps(row, sort_keys=True), flush=True)


def outcome(fn):
    try:
        fn()
    except OSError as exc:
        if isinstance(exc, socket.gaierror) or exc.errno in DENIED:
            return "DENIED", type(exc).__name__
        return "OTHER_ERROR", f"{type(exc).__name__}:{exc.errno}"
    return "ALLOWED", ""


def try_read(path):
    if os.path.isdir(path):
        os.listdir(path)
    else:
        with open(path, "rb") as fh:
            fh.read(1)


def try_write(path):
    try:
        os.makedirs(os.path.dirname(path))
    except FileExistsError:
        pass
    with open(path, "x") as fh:
        fh.write("planted")


def try_net(kind, host, port):
    if kind == "dns":
        socket.getaddrinfo(host, port)
    elif kind == "udp":
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(b"\0", (host, port))
    else:
        socket.create_connection((host, port), timeout=3).close()


def test_canary_probe():
    who = pwd.getpwuid(os.getuid()).pw_name
    emit(kind="identity", uid=os.getuid(), user=who)
    for name, path in READS.items():
        res, why = outcome(lambda p=path: try_read(p))
        emit(kind="read", probe=f"code read {name}", result=res, why=why,
             **{"pass": res == "DENIED"})
    for name, path in WRITES.items():
        res, why = outcome(lambda p=path: try_write(p))
        emit(kind="write", probe=f"code write {name}", result=res, why=why,
             **{"pass": res == "DENIED"})
    for name, (kind, host, port) in NETS.items():
        res, why = outcome(lambda k=kind, h=host, p=port: try_net(k, h, p))
        emit(kind="net", probe=f"code net {name}", result=res, why=why,
             **{"pass": res == "DENIED"})
"""


def source(reads: dict[str, str], writes: dict[str, str]) -> str:
    return (
        _BODY.replace("__MARK__", json.dumps(MARK))
        .replace("__READS__", json.dumps(reads))
        .replace("__WRITES__", json.dumps(writes))
        .replace("__NETS__", json.dumps(NET_TARGETS))
    )
