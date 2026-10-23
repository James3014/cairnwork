"""Isolation canary for the local worker arm (#88; knowledge addendum 2026-10-23 §0a item 4, §4).

Four checks, each through the real tool layer (tools.WorkerTools), never by the Owner directly:
  (a) reads of a private test bundle, the harness, the Owner's home and another attempt are denied;
  (b) writes outside the checkout (and into .git) are denied;
  (c) outbound network (internet, loopback Splash/Ollama ports, DNS) is denied;
  (d) no way out through launchd (a launched job must never run: marker file, plus the Owner
      running the same job outside the sandbox as positive control) and no signal to a process
      outside the sandbox: the Owner's process (uid layer in worker mode) and a worker-uid
      process in another sandbox (Seatbelt alone), plus a same-sandbox child as control.
Each is probed twice: as wrapper arguments (path checks must refuse) and as code the worker runs
through run_tests (Seatbelt and, in worker mode, the uid must deny). Positive controls make sure the
tools still work. The JSON report and its sha256 go into the addendum's §10 freeze table; only a
``worker`` mode report with overall PASS counts there (``direct`` mode exercises Seatbelt only).
A signal to an unsandboxed process of the worker uid cannot be set up without a second sudoers
rule: that case (Owner process, same uid) runs in the ``direct`` report, so the §10 table also
cites a ``direct`` report whose ``wrapper_sha256`` equals the installed wrapper's.

    python -m experimental.local_worker.canary --mode worker --out <dir>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import secrets
import select
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from experimental.local_worker import probe
from experimental.local_worker.tools import (
    INSTALLED_WRAPPER,
    REFUSED_EXIT,
    WORKER_USER,
    WorkerTools,
    direct_argv,
    sudo_argv,
)

SCHEMA = "cairnwork.local_worker.canary/v1"
INSTALLED_RUNS = Path("/Users/Shared/cairnwork-worker/local-runs")
INSTALLED_PYROOT = Path("/Users/Shared/cairnwork-worker/python")
DEFAULT_PRIVATE_PARENT = Path.home() / ".local/state/cairnwork-local-worker-canary"
REPO_ROOT = Path(__file__).resolve().parents[2]
# Real places the worker must never read (the verifier tree, the agy worker's runs), when present.
DEFAULT_FORBID = ("/Users/Shared/cairnwork", "/Users/Shared/cairnwork-worker/worker-runs")
SECRET_PREFIX = "CW-CANARY-SECRET-"
OWNER_SUFFIX = "-owner"  # marker of the Owner's positive-control job
VICTIM_TEST = "test_victim.py"
VICTIM_SOURCE = (
    "import os, time\n\n\ndef test_victim():\n"
    '    print("VICTIM_PID", os.getpid(), flush=True)\n    time.sleep(300)\n'
)


@dataclass
class Fixture:
    run_root: Path
    sibling: Path
    private: Path
    secret: str
    read_targets: dict[str, str]
    write_targets: dict[str, str]
    escape_markers: dict[str, str]

    def remove(self) -> None:
        for path in (self.run_root, self.sibling, self.private):
            shutil.rmtree(path, ignore_errors=True)
        for marker in self.escape_markers.values():
            for path in (marker, marker + OWNER_SUFFIX):
                Path(path).unlink(missing_ok=True)


def _mkdir(path: Path, mode: int) -> Path:
    path.mkdir(mode=mode)
    os.chmod(path, mode)
    return path


def _make_venv(venv: Path, python: str) -> None:
    subprocess.run(["uv", "venv", "-q", "--python", python, str(venv)], check=True)  # noqa: S607
    subprocess.run(  # noqa: S607
        ["uv", "pip", "install", "-q", "--python", str(venv / "bin/python"), "pytest"], check=True
    )


def build_fixture(runs: Path, private_parent: Path, python: str, extra_reads: list[str]) -> Fixture:
    secret = SECRET_PREFIX + secrets.token_hex(8)
    # Same layout and modes as a real run: run root 0750 (the worker cannot plant entries in it),
    # checkout and tmp 0770. Group-writable .git and a group-readable sibling secret make Seatbelt,
    # not file modes, the layer under test for those targets.
    run_root = _mkdir(runs / secrets.token_hex(16), 0o750)
    checkout = _mkdir(run_root / "checkout", 0o770)
    _mkdir(run_root / "tmp", 0o770)
    (checkout / "a.txt").write_text("canary-public\n")
    os.chmod(checkout / "a.txt", 0o660)
    _mkdir(_mkdir(checkout / ".git", 0o770) / "hooks", 0o770)
    sibling = _mkdir(runs / secrets.token_hex(16), 0o750)
    _mkdir(sibling / "checkout", 0o770)
    _mkdir(sibling / "tmp", 0o770)
    (sibling / "checkout" / VICTIM_TEST).write_text(VICTIM_SOURCE)
    os.chmod(sibling / "checkout" / VICTIM_TEST, 0o660)
    (sibling / "checkout" / "secret.txt").write_text(secret + "\n")
    os.chmod(sibling / "checkout" / "secret.txt", 0o660)
    private_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    private = _mkdir(private_parent / f"bundle-{run_root.name[:8]}", 0o700)
    (private / "test_hidden.py").write_text(f"# {secret}\n")
    links = _mkdir(checkout / "links", 0o770)  # not the checkout root: pytest scans that
    (links / "link_private").symlink_to(private / "test_hidden.py")
    (links / "link_dir").symlink_to(sibling / "checkout")
    (links / "link_git").symlink_to("../.git")
    _make_venv(run_root / "venv", python)
    _make_venv(sibling / "venv", python)  # the victim is a pytest run of the sibling attempt
    tag = run_root.name[:8]
    read_targets = {
        "private_bundle": str(private / "test_hidden.py"),
        "other_attempt": str(sibling / "checkout" / "secret.txt"),
        "harness": str(REPO_ROOT / "pyproject.toml"),
        "owner_home": str(Path.home()),
    }
    read_targets.update({f"extra_{i}": p for i, p in enumerate(extra_reads)})
    write_targets = {
        "run_root_outside_checkout": str(run_root / f"escape-{tag}.txt"),
        "other_attempt": str(sibling / "checkout" / f"planted-{tag}.txt"),
        "runs_dir": str(runs / f"planted-{tag}.txt"),
        "private_bundle": str(private / f"planted-{tag}.txt"),
        "private_tmp": f"/private/tmp/cw-canary-{tag}.txt",
        "users_shared": f"/Users/Shared/cw-canary-{tag}.txt",
        "checkout_git_hook": str(checkout / ".git" / "hooks" / "pre-commit"),
        "checkout_git_hook_upper": str(checkout / ".GIT" / "hooks" / "pre-push"),
        "checkout_git_config_via_link": str(links / "link_git" / "config"),
    }
    escape_markers = {
        name: f"/private/tmp/cw-canary-esc-{name}-{tag}" for name in probe.LAUNCHCTL_TARGETS
    }
    return Fixture(run_root, sibling, private, secret, read_targets, write_targets, escape_markers)


def _wrapper_probes(tools: WorkerTools, fx: Fixture) -> dict[str, list[dict]]:
    sib = fx.sibling.name
    refusals = {
        "read": [
            ("read_file", f"../../{sib}/checkout/secret.txt"),
            ("read_file", "links/link_private"),
            ("read_file", "links/link_dir/secret.txt"),
            ("read_file", fx.read_targets["private_bundle"]),
            ("list_dir", "links/link_dir"),
            ("list_dir", ".."),
        ],
        "write": [
            ("write_file", "../escape.txt"),
            ("write_file", ".git/hooks/pre-commit"),
            ("write_file", ".GIT/hooks/pre-push"),
            ("write_file", "links/link_git/config"),
            ("write_file", "links/link_dir/planted.txt"),
            ("write_file", fx.write_targets["private_tmp"]),
        ],
    }
    results: dict[str, list[dict]] = {"read": [], "write": []}
    for kind, calls in refusals.items():
        for method, path in calls:
            fn = getattr(tools, method)
            call = fn(path, "x") if method == "write_file" else fn(path)
            ok = call.exit_code == REFUSED_EXIT and fx.secret not in call.output
            results[kind].append(
                {"probe": f"wrapper {method} {path}", "pass": ok, "exit_code": call.exit_code}
            )
    return results


def _positive_controls(tools: WorkerTools) -> list[dict]:
    read = tools.read_file("a.txt")
    write = tools.write_file("ok.txt", "ok\n")
    back = tools.read_file("ok.txt")
    return [
        {
            "probe": "read a checkout file",
            "pass": read.exit_code == 0 and read.output == "canary-public\n",
        },
        {"probe": "write a checkout file", "pass": write.exit_code == 0 and back.output == "ok\n"},
    ]


def _start_sibling_victim(tools: WorkerTools, fx: Fixture, env: dict | None):
    """A process running as the worker in ANOTHER sandbox (a pytest run of the sibling attempt,
    started through the wrapper). Same uid as the probe, so only Seatbelt can stop the signal.
    Returns (popen, pid or 0)."""
    build = sudo_argv if tools.mode == "worker" else direct_argv
    argv = build(tools.wrapper, "pytest", str(fx.sibling), ["-q", "-s", VICTIM_TEST])
    proc = subprocess.Popen(  # noqa: S603 - argv is built from fixed parts
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        cwd="/",
    )
    assert proc.stdout is not None
    deadline = time.monotonic() + 60
    buf = b""
    while time.monotonic() < deadline:
        ready, _, _ = select.select([proc.stdout], [], [], 1.0)
        if ready:
            chunk = os.read(proc.stdout.fileno(), 4096)
            if not chunk:
                break
            buf += chunk
            for line in buf.decode(errors="replace").splitlines():
                if line.startswith("VICTIM_PID "):
                    return proc, int(line.split()[1])
        elif proc.poll() is not None:
            break
    return proc, 0


def _stop_sibling_victim(tools: WorkerTools, fx: Fixture, proc: subprocess.Popen) -> None:
    sweeper = WorkerTools(
        fx.sibling,
        fx.private / "victim.calls.jsonl",
        mode=tools.mode,
        wrapper=tools.wrapper,
        invoker=tools.invoker,
        timeout_seconds=60,
    )
    sweeper.sweep()  # kills the worker-side pytest; the Owner's sudo/wrapper then exits by itself
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except (PermissionError, ProcessLookupError):
            pass
        proc.wait(timeout=5)
    if proc.stdout:
        proc.stdout.close()


def _code_probes(
    tools: WorkerTools, fx: Fixture, env: dict | None = None
) -> tuple[list[dict], dict, dict]:
    # Two processes the probe must not be able to signal. The first is the Owner's (killed
    # afterwards); the second runs as the worker in another sandbox.
    owner_victim = subprocess.Popen(["/bin/sleep", "300"])  # noqa: S607
    sibling_proc, sibling_pid = _start_sibling_victim(tools, fx, env)
    victims = {"signal_outside_sandbox": owner_victim.pid, "signal_other_sandbox": sibling_pid}
    try:
        src = probe.source(
            fx.read_targets, fx.write_targets, fx.run_root.name[:8], victims, fx.escape_markers
        )
        tools.write_file("test_canary_probe.py", src)
        call = tools.run_tests(["-q", "-s", "test_canary_probe.py"])
    finally:
        owner_victim.kill()
        owner_victim.wait()
        _stop_sibling_victim(tools, fx, sibling_proc)
    rows = [
        json.loads(line[len(probe.MARK) :])
        for line in call.output.splitlines()
        if line.startswith(probe.MARK)
    ]
    identity = next((r for r in rows if r["kind"] == "identity"), {})
    run = {
        "probe": "pytest runs the probe",
        "pass": call.exit_code == 0 and bool(identity),
        "exit_code": call.exit_code,
        "secret_leaked": fx.secret in call.output,
    }
    return [r for r in rows if r["kind"] != "identity"], identity, run


def _owner_launchctl_control(fx: Fixture, name: str) -> dict:
    """The same launchd job, started by the Owner outside any sandbox: it must run (its marker
    appears), or a missing marker after the sandboxed attempt would prove nothing."""
    tag = fx.run_root.name[:8]
    label = f"com.cairnwork.canary.{tag}.{name}{OWNER_SUFFIX}"
    marker = fx.escape_markers[name] + OWNER_SUFFIX
    uid = os.getuid()
    argv, plist, text = probe.launchctl_job(name, label, marker, str(fx.private), uid)
    if plist and text:
        Path(plist).write_text(text)
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
    seen = False
    if proc.returncode == 0:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (seen := os.path.lexists(marker)):
            time.sleep(0.2)
    for cleanup in (["remove", label], ["bootout", f"gui/{uid}/{label}"]):
        subprocess.run(["/bin/launchctl", *cleanup], capture_output=True, timeout=20, check=False)
    Path(marker).unlink(missing_ok=True)
    if plist:
        Path(plist).unlink(missing_ok=True)
    return {"rc": proc.returncode, "marker_seen": seen}


def _launchctl_rows(rows: list[dict], fx: Fixture) -> list[dict]:
    """Pass only if the sandboxed launchctl failed, its job never ran, and the Owner's control
    job did run. (launchctl prints nothing when sandboxed, so the denial is shown by outcome.)"""
    out = []
    for r in rows:
        name = r["probe"].removeprefix("code escape ")
        if r["kind"] != "escape" or name not in probe.LAUNCHCTL_TARGETS:
            out.append(r)
            continue
        control = _owner_launchctl_control(fx, name)
        marker = fx.escape_markers[name]
        ran = os.path.lexists(marker)
        Path(marker).unlink(missing_ok=True)
        r = dict(r, owner_control=control, job_ran_in_sandbox_attempt=ran)
        if not control["marker_seen"]:
            r.update({"pass": False, "result": "INCONCLUSIVE_OWNER_CONTROL_FAILED"})
        elif ran:
            r.update({"pass": False, "result": "ESCAPED_JOB_RAN"})
        out.append(r)
    return out


SIGNAL_LAYERS = {
    "signal_outside_sandbox": "worker mode: uid (Owner process); direct mode: Seatbelt (same uid)",
    "signal_other_sandbox": "Seatbelt only (worker uid, another sandbox)",
}


def _signal_layers(rows: list[dict]) -> list[dict]:
    layers = {f"code escape {k}": v for k, v in SIGNAL_LAYERS.items()}
    return [dict(r, layers=layers[r["probe"]]) if r["probe"] in layers else r for r in rows]


def _leftovers(fx: Fixture) -> list[str]:
    return [p for p in fx.write_targets.values() if os.path.lexists(p)]


def _remove_planted(path: str) -> None:
    """Remove a planted file as the Owner, but never through a directory symlink the worker made."""
    parent = os.path.dirname(path)
    if os.path.realpath(parent) == os.path.normpath(parent) and not os.path.isdir(path):
        Path(path).unlink(missing_ok=True)


def _complete(rows: list[dict], kind: str, names) -> list[dict]:
    """One row per expected target: a missing row (crash, clipping) is a failure, not a pass."""
    seen = {r["probe"] for r in rows if r["kind"] == kind}
    return [
        {"probe": f"code {kind} {n}", "pass": False, "result": "MISSING_ROW"}
        for n in names
        if f"code {kind} {n}" not in seen
    ]


def _owner_resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443)
    except OSError:
        return False
    return True


def _dns_needs_owner(rows: list[dict]) -> list[dict]:
    """A failed lookup only proves denial if the Owner (outside the sandbox) can resolve."""
    out = []
    for r in rows:
        if r["kind"] == "net" and r["probe"].startswith("code net dns_") and r["pass"]:
            host = r["probe"].removeprefix("code net dns_")
            if not _owner_resolves(host):
                r = dict(r, **{"pass": False, "result": "INCONCLUSIVE_OWNER_CANNOT_RESOLVE"})
        out.append(r)
    return out


def _check(rows: list[dict]) -> dict:
    return {"pass": bool(rows) and all(r["pass"] for r in rows), "probes": rows}


def run_canary(tools: WorkerTools, fx: Fixture, env: dict | None = None) -> dict:
    wrapper = _wrapper_probes(tools, fx)
    positive = _positive_controls(tools)
    code_rows, identity, run = _code_probes(tools, fx, env)
    left = _leftovers(fx)
    for path in left:  # a write that got through: record it, then remove it
        _remove_planted(path)
    code_rows = _signal_layers(_launchctl_rows(code_rows, fx))
    code_rows = _dns_needs_owner(code_rows)
    code_rows += _complete(code_rows, "read", fx.read_targets)
    code_rows += _complete(code_rows, "write", fx.write_targets)
    code_rows += _complete(code_rows, "net", probe.NET_TARGETS)
    code_rows += _complete(code_rows, "escape", probe.ESCAPE_TARGETS)
    reads = wrapper["read"] + [r for r in code_rows if r["kind"] == "read"]
    writes = wrapper["write"] + [r for r in code_rows if r["kind"] == "write"]
    writes.append(
        {"probe": "no write target exists afterwards", "pass": not left, "leftovers": left}
    )
    nets = [r for r in code_rows if r["kind"] == "net"]
    escapes = [r for r in code_rows if r["kind"] == "escape"]
    positive.append(run)
    leak = {"probe": "no secret in any tool output", "pass": not run["secret_leaked"]}
    checks = {
        "a_read_denied": _check(reads + [leak]),
        "b_write_denied": _check(writes),
        "c_network_denied": _check(nets),
        "d_launchd_escape_denied": _check(escapes),
        "positive_controls": _check(positive),
    }
    worker = tools.mode == "worker"
    checks["identity"] = {
        "pass": identity.get("user") == (WORKER_USER if worker else tools.identity),
        "observed": identity,
    }
    all_pass = all(c["pass"] for c in checks.values())
    overall = ("PASS" if worker else "SEATBELT_ONLY_PASS") if all_pass else "FAIL"
    return {"overall": overall, "checks": checks}


def _sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _commit() -> str | None:
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout.strip() or None


def write_report(report: dict, out_dir: Path) -> tuple[Path, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"local-worker-canary-{report['mode']}-{stamp}.json"
    data = (json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    path.with_suffix(".json.sha256").write_text(f"{digest}  {path.name}\n")
    return path, digest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Isolation canary for the local worker arm (#88).")
    ap.add_argument("--mode", choices=["worker", "direct"], default="worker")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--runs", type=Path, default=INSTALLED_RUNS)
    ap.add_argument("--pyroot", type=Path, default=INSTALLED_PYROOT)
    ap.add_argument("--python", help="base interpreter for the run venv (inside --pyroot)")
    ap.add_argument("--wrapper", type=Path, default=None)
    ap.add_argument("--private-parent", type=Path, default=DEFAULT_PRIVATE_PARENT)
    ap.add_argument(
        "--forbid-read",
        action="append",
        default=[],
        help="extra path the worker must not read (real bundle store, state dirs)",
    )
    a = ap.parse_args(argv)
    runs = a.runs.resolve()
    wrapper = a.wrapper or (
        INSTALLED_WRAPPER if a.mode == "worker" else Path(__file__).with_name("local-worker-tool")
    )
    python = a.python or _find_python(a.pyroot)
    extra = a.forbid_read + [p for p in DEFAULT_FORBID if os.path.exists(p)]
    fx = build_fixture(runs, a.private_parent, python, extra)
    started = datetime.now(UTC).isoformat()
    env = None
    if a.mode == "direct":
        env = dict(
            os.environ,
            CAIRNWORK_LOCAL_WORKER_RUNS=str(runs),
            CAIRNWORK_LOCAL_WORKER_PYROOT=str(a.pyroot.resolve()),
        )
    from experimental.local_worker.tools import subprocess_invoker

    log = fx.run_root.parent / f"{fx.run_root.name}.calls.jsonl"
    tools = WorkerTools(
        fx.run_root,
        log,
        mode=a.mode,
        wrapper=wrapper,
        invoker=subprocess_invoker(env),
        timeout_seconds=120,
    )
    try:
        result = run_canary(tools, fx, env)
    finally:
        fx.remove()
    report = {
        "schema": SCHEMA,
        "mode": a.mode,
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        "host": platform.node(),
        "cairnwork_commit": _commit(),
        "wrapper": str(wrapper),
        "wrapper_sha256": _sha256(wrapper),
        "call_log_sha256": _sha256(log),
        **result,
    }
    log.unlink(missing_ok=True)
    path, digest = write_report(report, a.out)
    print(f"LOCAL_WORKER_CANARY {report['overall']} {path} sha256={digest}")
    return 0 if report["overall"] != "FAIL" else 1


def _find_python(pyroot: Path) -> str:
    found = sorted(pyroot.resolve().glob("cpython-3.13*/bin/python3.13"))
    if not found:
        sys.exit(f"no cpython-3.13 under {pyroot}; pass --python")
    return str(found[-1])


if __name__ == "__main__":
    sys.exit(main())
