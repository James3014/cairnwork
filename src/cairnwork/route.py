"""``cairnwork-route``: the command the old Gateway calls (council F).

Every command prints one JSON object on stdout. Exit codes: 0 done; 2 refused
(``{"error": code, "detail": ...}``: not accepting, bad request, not cancellable,
recovery busy, key reused, card already active); 3 unknown task; 1 anything else,
including a recovery that could not settle every task. Only NOT_ACCEPTING means
"not taken, the old system may run it"; after any other answer the caller must
not run the task elsewhere (exit 1 and 3 mean "unknown", never "failed").

    cairnwork-route --state DIR check < request.json   # shadow mode: validate only
    cairnwork-route --state DIR submit < request.json
    cairnwork-route --state DIR status (--task-id ID | --key KEY)
    cairnwork-route --state DIR cancel --task-id ID
    cairnwork-route --state DIR in-flight        # for the deployer's quiescence gate
    cairnwork-route --state DIR accept | drain   # switch new-task intake on / off
    cairnwork-route --state DIR recover          # after a restart, with no runner alive
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from cairnwork.intake import Intake, IntakeRefused, task_from_payload
from cairnwork.ownership import (
    DuplicateLogicalTask,
    KeyReusedWithDifferentRequest,
    OwnerMismatch,
    OwnershipUnknown,
)
from cairnwork.pipeline import TaskRejected, check_task, run_bound_task
from cairnwork.recovery import recover
from cairnwork.runlock import RunLockBusy
from cairnwork.workers.agy import AgyWorker


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    intake = Intake(Path(args.state).resolve())
    try:
        result = _dispatch(intake, args)
    except OwnershipUnknown as exc:
        return _emit({"error": "UNKNOWN_TASK", "detail": str(exc)}, 3)
    except IntakeRefused as exc:
        return _emit({"error": exc.code, "detail": exc.detail}, 2)
    except TaskRejected as exc:
        return _emit({"error": exc.code, "detail": str(exc)}, 2)
    except RunLockBusy as exc:
        return _emit({"error": exc.code, "detail": "a runner is still working"}, 2)
    except (KeyReusedWithDifferentRequest, DuplicateLogicalTask, OwnerMismatch) as exc:
        return _emit({"error": type(exc).__name__, "detail": str(exc)}, 2)
    except Exception as exc:  # noqa: BLE001 - reported as unknown, never as success
        return _emit({"error": "INTERNAL", "detail": f"{type(exc).__name__}: {exc}"}, 1)
    stuck = [d for d in result.get("decisions", []) if d["reason"].startswith("RECOVERY_ERROR")]
    return _emit(result, 1 if stuck else 0)


def _dispatch(intake: Intake, args) -> Any:
    if args.command in ("check", "submit"):
        try:
            payload = json.loads(sys.stdin.read())
        except json.JSONDecodeError as exc:
            raise IntakeRefused("BAD_REQUEST", "stdin is not JSON") from exc
        if args.command == "check":  # no binding, no files, no runner
            check_task(task_from_payload(payload))
            return {"would_accept": intake.accepting()}
        return intake.submit(payload)
    if args.command == "status":
        return intake.status(task_id=args.task_id, key=args.key)
    if args.command == "cancel":
        return intake.cancel(args.task_id)
    if args.command == "in-flight":
        return intake.in_flight()
    if args.command in ("accept", "drain"):
        intake.set_accepting(args.command == "accept")
        return {"accepting": intake.accepting()}
    if args.command == "recover":
        decisions = recover(
            ledger=intake.ledger,
            notary=intake.notary,
            journal=intake.journal,
            run_root=intake.run_root,
            verifier_account=intake.verifier_account(),
        )
        return {"decisions": [d.__dict__ for d in decisions]}
    return _run(intake, args.task_id)


def _run(intake: Intake, task_id: str) -> dict[str, Any]:
    """The runner submit starts. It keeps the inherited lock fd open until it exits."""
    config = intake.config()
    result = run_bound_task(
        intake.load_request(task_id),
        task_id,
        ledger=intake.ledger,
        notary=intake.notary,
        journal=intake.journal,
        worker=AgyWorker(model=config["model"], agy=config["agy"]),
        run_root=intake.run_root,
        verifier_account=intake.verifier_account(),
    )
    return {k: v for k, v in result.__dict__.items() if k != "logs"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cairnwork-route")
    parser.add_argument("--state", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    commands.add_parser("submit")
    status = commands.add_parser("status")
    which = status.add_mutually_exclusive_group(required=True)
    which.add_argument("--task-id")
    which.add_argument("--key")
    commands.add_parser("cancel").add_argument("--task-id", required=True)
    for name in ("in-flight", "accept", "drain", "recover"):
        commands.add_parser(name)
    run = commands.add_parser("_run")
    run.add_argument("--task-id", required=True)
    run.add_argument("--lock-fd", type=int, required=True)  # held, never used directly
    return parser


def _emit(value: dict[str, Any], code: int) -> int:
    print(json.dumps(value, sort_keys=True, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main())
