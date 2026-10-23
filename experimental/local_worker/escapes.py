"""Escape checks of the isolation canary (#88, check d): launchd jobs and signals.

Split out of canary.py (400-line cap). Helpers only; canary.run_canary wires them in.
"""

from __future__ import annotations

import os
import select
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from experimental.local_worker import probe
from experimental.local_worker.tools import WorkerTools, direct_argv, sudo_argv

if TYPE_CHECKING:
    from experimental.local_worker.canary import Fixture

OWNER_SUFFIX = "-owner"  # marker of the Owner's positive-control job
VICTIM_TEST = "test_victim.py"
VICTIM_SOURCE = (
    "import os, time\n\n\ndef test_victim():\n"
    '    print("VICTIM_PID", os.getpid(), flush=True)\n    time.sleep(300)\n'
)


def start_sibling_victim(tools: WorkerTools, fx: Fixture, env: dict | None):
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


def stop_sibling_victim(tools: WorkerTools, fx: Fixture, proc: subprocess.Popen) -> None:
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
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass  # a refused signal; the sweep above already killed the worker side
    if proc.stdout:
        proc.stdout.close()


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


def unlink_quiet(path: str) -> None:
    """A marker an escaped job made belongs to the worker in sticky /private/tmp: not removable."""
    try:
        Path(path).unlink(missing_ok=True)
    except PermissionError:
        pass


def launchctl_rows(rows: list[dict], fx: Fixture) -> list[dict]:
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
        unlink_quiet(marker)
        r = dict(r, owner_control=control, job_ran_in_sandbox_attempt=ran)
        if ran:  # an escape outranks a failed control
            r.update({"pass": False, "result": "ESCAPED_JOB_RAN"})
        elif not control["marker_seen"]:
            r.update({"pass": False, "result": "INCONCLUSIVE_OWNER_CONTROL_FAILED"})
        out.append(r)
    return out


SIGNAL_LAYERS = {
    "signal_outside_sandbox": "worker mode: uid (Owner process); direct mode: Seatbelt (same uid)",
    "signal_other_sandbox": "Seatbelt only (worker uid, another sandbox)",
}


def signal_layers(rows: list[dict]) -> list[dict]:
    layers = {f"code escape {k}": v for k, v in SIGNAL_LAYERS.items()}
    return [dict(r, layers=layers[r["probe"]]) if r["probe"] in layers else r for r in rows]
