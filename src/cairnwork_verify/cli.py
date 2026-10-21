"""``cairnwork-verify``: judge a commit made elsewhere (see verify.py).

    cairnwork-verify --state DIR run --repo-path P --base SHA --candidate SHA \\
        --allowed-file F [...] --verifier CMD [...] [--test-commit SHA]
    cairnwork-verify --state DIR cleanup      # after a crashed run, before ``recover``

Prints one JSON object. Exit codes for ``run``: 0 verified; 2 refused or not verified
(``status`` and ``reason`` say which); 3 usage error; 1 internal error.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any, NoReturn

from cairnwork.intake import Intake, IntakeRefused
from cairnwork.pipeline import TaskRejected
from cairnwork_verify.verify import cleanup, verify_candidate


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        _emit({"error": "USAGE", "detail": message}, 3)
        sys.exit(3)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    intake = Intake(Path(args.state).resolve())
    try:
        if args.command == "cleanup":
            return _emit(cleanup(intake), 0)
        result = _run(intake, args)
    except IntakeRefused as exc:
        return _emit({"error": exc.code, "detail": exc.detail}, 2)
    except TaskRejected as exc:
        return _emit({"error": exc.code, "detail": str(exc)}, 2)
    except Exception as exc:  # noqa: BLE001 - reported as unknown, never as success
        return _emit({"error": "INTERNAL", "detail": f"{type(exc).__name__}: {exc}"}, 1)
    return _emit(result, 0 if result["verified"] else 2)


def _run(intake: Intake, args) -> dict[str, Any]:
    try:
        verifiers = tuple(
            (f"pytest-{i}", tuple(shlex.split(command))) for i, command in enumerate(args.verifier)
        )
    except ValueError as exc:
        raise TaskRejected("BAD_VERIFIER", str(exc)) from exc
    return verify_candidate(
        intake,
        repo=Path(args.repo_path).resolve(),
        base=args.base,
        candidate=args.candidate,
        allowed_files=tuple(args.allowed_file),
        verifiers=verifiers,
        test_commit=args.test_commit,
    )


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="cairnwork-verify")
    parser.add_argument("--state", required=True)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    commands.add_parser("cleanup")
    run = commands.add_parser("run")
    run.add_argument("--repo-path", required=True)
    run.add_argument("--base", required=True)
    run.add_argument("--candidate", required=True)
    run.add_argument("--allowed-file", action="append", required=True)
    run.add_argument("--verifier", action="append", required=True)
    run.add_argument("--test-commit", default="")
    return parser


def _emit(value: dict[str, Any], code: int) -> int:
    print(json.dumps(value, sort_keys=True, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main())
