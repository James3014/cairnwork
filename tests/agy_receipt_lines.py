"""Recorded dispatcher stderr with worker receipts, in the exact shape nexus-agy-dispatch
writes them (Nexus-new #2888: ``NEXUS_AGY_DISPATCH `` + json.dumps(..., sort_keys=True),
one WORKER_LAUNCH, WORKER_SWEEP and WORKER_CLEANUP line per attempt, the final status
line last, the provider's own dispatcher-shaped lines relayed with ``[provider] ``)."""

import json
import os

PREFIX = "NEXUS_AGY_DISPATCH "
WORKER = "_cairnwork_worker"
RUN_TAG = "0123456789abcdef" * 2
WRAPPER_SHA = "5" * 64


def line(obj: dict) -> str:
    return PREFIX + json.dumps(obj, sort_keys=True) + "\n"


def launch(run_root: str, *, attempt=1, user=WORKER, run_root_id=None) -> str:
    """run_root_id defaults to the directory's real (dev, ino) when it exists."""
    if run_root_id is None:
        info = os.lstat(run_root) if os.path.lexists(run_root) else None
        run_root_id = [info.st_dev, info.st_ino] if info else [16777231, 4242]
    return line(
        {
            "event": "WORKER_LAUNCH",
            "attempt": attempt,
            "operation_id": None,
            "run_as_user": user,
            "run_root": run_root,
            "run_root_id": run_root_id,
            "pid": None,
            "lstart": None,
            "wrapper_sha256": WRAPPER_SHA,
        }
    )


def sweep(*, attempt=1, result="clean", wrapper="CLEAN", left=()) -> str:
    obj = {"event": "WORKER_SWEEP", "attempt": attempt, "result": result}
    return line(obj | {"wrapper": wrapper, "left": list(left)})


def cleanup(*, attempt=1, removed=True, failure=None) -> str:
    obj = {"event": "WORKER_CLEANUP", "attempt": attempt, "run_root_removed": removed}
    return line(obj | {"worker_failure": failure})


def status(state="completed", *, attempts=1, **extra) -> str:
    return line({"status": state, "attempts": attempts, **extra})


def good_run(run_root: str, **launch_args) -> str:
    """A clean run: launch, the provider's own stderr, sweep, cleanup, completed."""
    return (
        launch(run_root, **launch_args)
        + "agy: some provider diagnostics\n"
        + sweep()
        + cleanup()
        + status()
    )
