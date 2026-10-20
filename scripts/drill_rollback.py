"""Cairnwork-side cutover and rollback drill (council clause F), via ``cairnwork-route``.

Usage: uv run python scripts/drill_rollback.py <empty work dir> [model]

Walks the Cairnwork half of docs/GATEWAY_ROUTE.md "cutover and rollback" with a
real Agy worker: shadow ``check``, ``accept``, submit task A, ``drain`` while A
runs, then a new key must be refused (NOT_ACCEPTING), a resend of A must return
the same task, ``in-flight`` must count A, A must finish here and never re-run,
and ``recover`` must have nothing to decide. Each step is printed and recorded;
the JSON goes to docs/drills/<date>-rollback-cairnwork-side.json.

This is NOT the full rollback drill: the Gateway side (switch, forwarding,
deployer gate) is not part of it.
"""

from __future__ import annotations

import datetime
import json
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import drill_normal

TIMEOUT = 600.0
RESULTS: list[dict[str, Any]] = []
Json = dict[str, Any]


def route(state: Path, *args: str, stdin: Json | None = None) -> tuple[int, Json]:
    proc = subprocess.run(
        [sys.executable, "-m", "cairnwork.route", "--state", str(state), *args],
        input=json.dumps(stdin) if stdin is not None else "",
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        out = {"unparsed_stdout": proc.stdout, "stderr": proc.stderr[-500:]}
    return proc.returncode, out


def record(step: str, expected: str, ok: bool, **data: Any) -> bool:
    RESULTS.append({"step": step, "expected": expected, "ok": bool(ok), **data})
    print(json.dumps(RESULTS[-1], sort_keys=True, default=str), flush=True)
    return bool(ok)


def request(repo: Path, base: str, key: str) -> Json:
    return {
        "idempotency_key": key,
        "card_hash": f"card-{key}",
        "title": "average() returns the wrong value",
        "instructions": "average([2, 4]) returns 2.0 but should return 3. Fix average() in "
        "src/stats.py so it returns the arithmetic mean.",
        "repo": str(repo),
        "base_commit": base,
        "allowed_files": ["src/stats.py"],
        "verifiers": [["pytest", ["python3", "-m", "pytest", "-q", "tests/test_stats.py"]]],
    }


def wait_for(state: Path, key: str, done: Callable[[Json], bool]) -> Json:
    deadline = time.monotonic() + TIMEOUT
    last: Json = {}
    while time.monotonic() < deadline:
        code, last = route(state, "status", "--key", key)
        if code == 0 and done(last):
            return last
        time.sleep(0.2)
    return {**last, "timed_out": True}


def step_shadow(state: Path, req: Json) -> bool:
    code, out = route(state, "check", stdin=req)
    leftovers = sorted(p.name for p in state.iterdir())
    ok = code == 0 and out.get("would_accept") is False and leftovers == []
    return record(
        "1 shadow check",
        "exit 0, would_accept false, state dir still empty",
        ok,
        exit=code,
        output=out,
        state_files_after=leftovers,
    )


def step_accept_submit(state: Path, req: Json, model: str) -> tuple[bool, Json]:
    config = {"model": model, "verifier_account": drill_normal.ACCOUNT is not None}
    state.joinpath("config.json").write_text(json.dumps(config))
    state.joinpath("config.json").chmod(0o600)
    code, out = route(state, "accept")
    ok = record(
        "2a accept",
        "exit 0, accepting true",
        code == 0 and out.get("accepting") is True,
        exit=code,
        output=out,
    )
    code, sub = route(state, "submit", stdin=req)
    ok &= record(
        "2b submit A",
        "exit 0, duplicate false",
        code == 0 and sub.get("duplicate") is False,
        exit=code,
        output=sub,
    )
    running = wait_for(state, req["idempotency_key"], lambda s: s["status"] != "BOUND")
    ok &= record(
        "2c A dispatched",
        "status DISPATCHED before drain",
        running.get("status") == "DISPATCHED",
        status=running,
    )
    code, out = route(state, "drain")
    ok &= record(
        "2d drain while A runs",
        "exit 0, accepting false",
        code == 0 and out.get("accepting") is False,
        exit=code,
        output=out,
    )
    return ok, {"task_id": sub.get("task_id"), "attempt": running.get("attempt_id")}


def step_after_drain(state: Path, req: Json, base: str, a: Json) -> bool:
    other = request(Path(req["repo"]), base, "rollback-new-key")
    code, out = route(state, "submit", stdin=other)
    ok = record(
        "3a new key after drain",
        "exit 2, NOT_ACCEPTING",
        code == 2 and out.get("error") == "NOT_ACCEPTING",
        exit=code,
        output=out,
    )
    code, out = route(state, "submit", stdin=req)
    ok &= record(
        "3b resend of A after drain",
        "exit 0, same task id, duplicate true",
        code == 0 and out.get("task_id") == a["task_id"] and out.get("duplicate") is True,
        exit=code,
        output=out,
    )
    code, out = route(state, "in-flight")
    ok &= record(
        "3c in-flight while A runs",
        "exit 0, count >= 1, A listed, accepting false",
        code == 0
        and out.get("count", 0) >= 1
        and a["task_id"] in out.get("task_ids", [])
        and out.get("accepting") is False,
        exit=code,
        output=out,
    )
    return ok


def step_finish(state: Path, req: Json, a: Json) -> bool:
    final = wait_for(
        state, req["idempotency_key"], lambda s: s["status"] not in ("BOUND", "DISPATCHED")
    )
    status, reason = final.get("status"), final.get("detail", "")
    finished = status == "COMPLETED" or (status == "FAILED" and bool(reason))
    ok = record(
        "4a A finishes in Cairnwork",
        "COMPLETED, or FAILED with a reason; same attempt as when drained",
        finished and final.get("attempt_id") == a["attempt"] and not final.get("timed_out"),
        status=final,
        attempt_when_dispatched=a["attempt"],
    )
    code, out = route(state, "in-flight")
    ok &= record(
        "4b in-flight after A",
        "exit 0, count 0",
        code == 0 and out.get("count") == 0,
        exit=code,
        output=out,
    )
    code, out = route(state, "recover")
    ok &= record(
        "4c recover",
        "exit 0, no decisions",
        code == 0 and out.get("decisions") == [],
        exit=code,
        output=out,
    )
    code, out = route(state, "submit", stdin=req)
    ok &= record(
        "4d resend after finish",
        "exit 0, same task id, duplicate true (no re-run)",
        code == 0 and out.get("task_id") == a["task_id"] and out.get("duplicate") is True,
        exit=code,
        output=out,
    )
    return ok


def main() -> int:
    work = Path(sys.argv[1]).resolve()
    model = sys.argv[2] if len(sys.argv) > 2 else "gemini-3.8-flash-high"
    if work.exists() and any(work.iterdir()):
        raise SystemExit(f"{work} must be empty")
    repo = work / "project"
    base = drill_normal.make_project(repo)
    state = work / "state"
    state.mkdir(mode=0o700)
    state.chmod(0o700)
    req = request(repo, base, "rollback-a")
    passed = step_shadow(state, req)
    ok, a = step_accept_submit(state, req, model)
    passed &= ok
    if ok:
        passed &= step_after_drain(state, req, base, a)
        passed &= step_finish(state, req, a)
    date = datetime.date.today().isoformat()
    out = {
        "passed": bool(passed),
        "scope": "Cairnwork side only; Gateway switch, forwarding and deployer gate not exercised",
        "date": date,
        "model": model,
        "verifier_account_used": drill_normal.ACCOUNT is not None,
        "steps": RESULTS,
    }
    target = Path(__file__).resolve().parent.parent / "docs" / "drills"
    (target / f"{date}-rollback-cairnwork-side.json").write_text(
        json.dumps(out, indent=2, sort_keys=True, default=str) + "\n"
    )
    print(json.dumps({"passed": out["passed"]}))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
