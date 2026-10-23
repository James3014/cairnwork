"""The code the canary makes the worker run (through run_tests): it tries every forbidden read,
write and connection itself and prints one ``CANARY_PROBE <json>`` line per attempt. It never
fails, so a pytest exit code other than 0 means the probe could not run at all."""

from __future__ import annotations

import inspect
import json
import os

MARK = "CANARY_PROBE "

NET_TARGETS = {
    "internet_tcp_1.1.1.1:443": ("tcp", "1.1.1.1", 443),
    "loopback_splash_127.0.0.1:8000": ("tcp", "127.0.0.1", 8000),
    "loopback_gateway_127.0.0.1:11434": ("tcp", "127.0.0.1", 11434),
    "udp_8.8.8.8:53": ("udp", "8.8.8.8", 53),
    "dns_github.com": ("dns", "github.com", 443),
}

# Ways out of the Seatbelt profile through launchd: a job launchd starts is not a child of the
# sandboxed process, so it would run as the worker without the profile (no network deny).
# Also: signalling a process outside the sandbox (the agy worker's runs share the worker uid).
# A launched job proves nothing by its exit code (launchctl fails silently inside ANY sandbox-exec,
# even a plain allow-default one), so the job touches a marker the sandbox forbids the worker to
# write: the canary passes the row only if the marker never appears AND the Owner, running the
# same job outside the sandbox, does produce it (positive control).
LAUNCHCTL_TARGETS = ("launchctl_submit", "launchctl_bootstrap_gui_domain")
# Signals: the Owner's process (uid layer, plus Seatbelt in direct mode), a worker-uid process in a
# different sandbox (Seatbelt alone: same uid), and a child in the probe's own sandbox, which must
# be signalable (positive control: proves os.kill works at all).
SIGNAL_TARGETS = ("signal_outside_sandbox", "signal_other_sandbox")
SIGNAL_CONTROL = "signal_same_sandbox_control"
ESCAPE_TARGETS = (*LAUNCHCTL_TARGETS, *SIGNAL_TARGETS, SIGNAL_CONTROL)


def launchctl_job(name, label, marker, tmpdir, uid):
    """argv (and plist text, if any) of a launchd job that touches ``marker``. The canary runs
    this same function's source inside the sandbox and, as the Owner, outside it."""
    if name == "launchctl_submit":
        return ["/bin/launchctl", "submit", "-l", label, "--", "/usr/bin/touch", marker], None, None
    plist = os.path.join(tmpdir, label + ".plist")
    text = (
        '<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>'
        f"<key>Label</key><string>{label}</string><key>RunAtLoad</key><true/>"
        "<key>ProgramArguments</key><array><string>/usr/bin/touch</string>"
        f"<string>{marker}</string></array></dict></plist>"
    )
    return ["/bin/launchctl", "bootstrap", f"gui/{uid}", plist], plist, text


_BODY = r"""
import errno
import json
import os
import pwd
import signal
import socket
import subprocess

MARK = __MARK__
READS = __READS__
WRITES = __WRITES__
NETS = __NETS__
ESCAPES = __ESCAPES__
TAG = __TAG__
VICTIMS = __VICTIMS__
MARKERS = __MARKERS__
SIGNAL_CONTROL = __SIGNAL_CONTROL__
DENIED = (errno.EPERM, errno.EACCES)

__JOB__


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


def try_signal(pid):
    try:
        os.kill(pid, 0)  # existence check first: a signal to a dead pid proves nothing
        os.kill(pid, signal.SIGCONT)
    except OSError as exc:
        if exc.errno in DENIED:
            return "DENIED", type(exc).__name__
        return "OTHER_ERROR", f"{type(exc).__name__}:{exc.errno}"
    return "ALLOWED", "SIGCONT delivered"


def try_escape(name):
    if name == SIGNAL_CONTROL:
        child = subprocess.Popen(["/bin/sleep", "60"])
        try:
            return try_signal(child.pid)
        finally:
            child.kill()
            child.wait()
    if name.startswith("signal_"):
        pid = VICTIMS.get(name, 0)
        if pid < 2:  # never kill(0) or kill(-1): they would signal this process group or every pid
            return "OTHER_ERROR", "no victim process"
        return try_signal(pid)
    label = f"com.cairnwork.canary.{TAG}.{name}"
    argv, plist, text = launchctl_job(
        name, label, MARKERS[name], os.environ.get("TMPDIR", "."), os.getuid()
    )
    if plist:
        with open(plist, "w") as fh:
            fh.write(text)
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    if proc.returncode == 0:
        for lab in (label, label + ".b"):
            subprocess.run(["/bin/launchctl", "remove", lab], capture_output=True, timeout=20)
        return "ALLOWED", "launchctl accepted the job (removed again)"
    return "DENIED", f"rc={proc.returncode} {proc.stderr.strip()[:200]}"


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
    for name in ESCAPES:
        try:
            res, why = try_escape(name)
        except Exception as exc:  # a crash is not a denial
            res, why = "OTHER_ERROR", repr(exc)[:200]
        want = "ALLOWED" if name == SIGNAL_CONTROL else "DENIED"
        emit(kind="escape", probe=f"code escape {name}", result=res, why=why,
             **{"pass": res == want})
"""


def source(
    reads: dict[str, str],
    writes: dict[str, str],
    tag: str,
    victims: dict[str, int],
    markers: dict[str, str],
) -> str:
    return (
        _BODY.replace("__JOB__", inspect.getsource(launchctl_job))
        .replace("__MARK__", json.dumps(MARK))
        .replace("__READS__", json.dumps(reads))
        .replace("__WRITES__", json.dumps(writes))
        .replace("__NETS__", json.dumps(NET_TARGETS))
        .replace("__ESCAPES__", json.dumps(list(ESCAPE_TARGETS)))
        .replace("__TAG__", json.dumps(tag))
        .replace("__VICTIMS__", json.dumps({k: int(v) for k, v in victims.items()}))
        .replace("__MARKERS__", json.dumps(markers))
        .replace("__SIGNAL_CONTROL__", json.dumps(SIGNAL_CONTROL))
    )
