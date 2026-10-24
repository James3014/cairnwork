#!/usr/bin/env python3
"""Check a Cairnwork state before running it (#234): one JSON object, one code per check.

    uv run python scripts/preflight.py --state DIR [--clone PATH]

Checks (each ``{"code", "detail"}``, code ``OK`` when it passes):

* ``config``: the state directory exists (``STATE_MISSING``) and its config.json loads (else its
  IntakeRefused code, e.g. ``BAD_CONFIG``); the other
  checks need it and are ``SKIPPED`` when it fails;
* ``verifier_python``: the configured interpreter (or the account's) exists and is executable
  (``VERIFIER_PYTHON_MISSING``);
* ``account``: the verifier account can run it (``VERIFIER_ACCOUNT_UNUSABLE``, with the first
  directory it cannot traverse); ``walk`` lists every directory checked; when the account's lock
  stays busy for ``--lock-wait`` seconds (or its queue is unreadable) ``available()`` never ran,
  so the result is ``ACCOUNT_CHECK_INCONCLUSIVE`` (not a pass; wait longer and run it again);
* ``mirror``: the configured mirror is a directory (``MIRROR_MISSING``);
* ``clone``: with ``--clone``, it is the state's mirror by real path (``CLONE_NOT_STATE_MIRROR``,
  the check ``cairnwork-issues`` makes at startup);
* ``worker_isolation``: a configured ``agy_run_as_user`` really is another uid than the owner and
  the verifier account (``WORKER_NOT_ISOLATED``); unset is OK (the worker path then never says
  UNCOMPARABLE).

``ok`` is true only when every check is OK; the exit code is 0 then and 1 otherwise. Read-only for
the state: nothing is burned, fetched or bound. The account check, like every verifier run, takes
the account's lock (at most ``--lock-wait`` seconds) and starts one probe as the account.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from cairnwork.account_check import blocked, precheck, walk
from cairnwork.deferred_tests import BaseUncomparable
from cairnwork.intake import Intake, IntakeRefused
from cairnwork.route import agy_dispatch
from cairnwork.workers.agy import AgyWorker

OK = "OK"
INCONCLUSIVE = "ACCOUNT_CHECK_INCONCLUSIVE"  # the lock stayed busy: available() did not run


def _result(code: str, detail: str = "", **extra: Any) -> dict[str, Any]:
    return {"code": code, "detail": detail, **extra}


def _account(intake: Intake, python: str | None, lock_wait: float) -> dict[str, Any]:
    account = intake.verifier_account()
    if account is None:
        return _result(OK, "no verifier account configured: verifiers run as the owner")
    py = python or account.python
    steps = walk(py, account.user)
    try:
        deferred = precheck(account, py, timeout=lock_wait)
    except BaseUncomparable as exc:
        return _result(exc.comparison.code, exc.comparison.detail, walk=steps)
    if deferred is not None:  # available() never ran: not a pass (wait longer: --lock-wait)
        hint = blocked(py, account.user) or "no blocked directory found"
        return _result(INCONCLUSIVE, f"{deferred}; {hint}", walk=steps)
    return _result(OK, f"{account.user} can run {py}", walk=steps)


def _python(python: str | None) -> dict[str, Any]:
    if python is None:
        return _result(OK, f"no verifier python configured: {sys.executable}")
    if os.path.isfile(python) and os.access(python, os.X_OK):
        return _result(OK, python)
    return _result("VERIFIER_PYTHON_MISSING", f"{python} is not an executable file")


def _mirror(config: dict[str, Any]) -> dict[str, Any]:
    mirror = config["mirror"]
    if mirror is None:
        return _result(OK, "no mirror configured: tasks use the caller's repository")
    if not Path(mirror).is_dir():
        return _result("MIRROR_MISSING", f"mirror {mirror} does not exist")
    return _result(OK, mirror)


def _clone(intake: Intake, clone: str | None) -> dict[str, Any]:
    if clone is None:
        return _result(OK, "no --clone given")
    problem = intake.clone_problem(Path(clone))
    code, _, detail = problem.partition(": ")
    return _result(code, detail) if problem else _result(OK, clone)


def _isolation(intake: Intake, config: dict[str, Any]) -> dict[str, Any]:
    if config["agy_run_as_user"] is None:
        return _result(OK, "agy_run_as_user is not set: worker-path UNCOMPARABLE reads as FAILED")
    user = getattr(intake.verifier_account(), "user", None)
    worker = AgyWorker(dispatch=agy_dispatch(config), verifier_user=user)
    why = worker.not_isolated_because
    return _result("WORKER_NOT_ISOLATED", why) if why else _result(OK, "isolated")


def preflight(state: Path, clone: str | None, lock_wait: float = 60.0) -> dict[str, Any]:
    intake = Intake(state)
    names = ("verifier_python", "account", "mirror", "clone", "worker_isolation")
    try:
        if not state.is_dir():
            raise IntakeRefused("STATE_MISSING", f"{state} is not a directory")
        config = intake.config()
    except IntakeRefused as exc:
        skipped = {name: _result("SKIPPED", "config did not load") for name in names}
        return {"ok": False, "checks": {"config": _result(exc.code, exc.detail), **skipped}}
    python = config["verifier_python"] or getattr(intake.verifier_account(), "python", None)
    checks = {
        "config": _result(OK, str(state / "config.json")),
        "verifier_python": _python(python),
        "account": _account(intake, python, lock_wait),
        "mirror": _mirror(config),
        "clone": _clone(intake, clone),
        "worker_isolation": _isolation(intake, config),
    }
    return {"ok": all(c["code"] == OK for c in checks.values()), "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="preflight")
    parser.add_argument("--state", required=True)
    parser.add_argument("--clone")
    parser.add_argument("--lock-wait", type=float, default=60.0)
    args = parser.parse_args(argv)
    report = preflight(Path(args.state).resolve(), args.clone, args.lock_wait)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] is True else 1


if __name__ == "__main__":
    sys.exit(main())
