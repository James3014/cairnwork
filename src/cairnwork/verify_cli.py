"""``cairnwork-verify``: judge a commit made elsewhere (see verify.py).

    cairnwork-verify --state DIR run --repo-path P --base SHA --candidate SHA \\
        --allowed-file F [...] --verifier CMD [...] [--test-commit SHA]
    cairnwork-verify --state DIR import-tests --repo-path P --bundle FILE
        # keep a deferred test commit in the private store (deferred_store.py), not on a remote
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

from cairnwork.deferred_tests import TestCommitRejected
from cairnwork.intake import Intake, IntakeRefused
from cairnwork.pipeline import TaskRejected
from cairnwork.verify_candidate import cleanup, verify_candidate


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
        if args.command == "import-tests":
            return _import_tests(intake, args)
        result = _run(intake, args)
    except IntakeRefused as exc:
        return _emit({"error": exc.code, "detail": exc.detail}, 2)
    except TaskRejected as exc:
        return _emit({"error": exc.code, "detail": str(exc)}, 2)
    except Exception as exc:  # noqa: BLE001 - reported as unknown, never as success
        return _emit({"error": "INTERNAL", "detail": f"{type(exc).__name__}: {exc}"}, 1)
    return _emit(result, 0 if result["verified"] else 2)


def _import_tests(intake: Intake, args) -> int:
    intake.prepare()
    repo = Path(args.repo_path).resolve()
    try:
        sha = intake.test_store().import_bundle(
            repo, Path(args.bundle).resolve(), intake.state / "verify-used-tests"
        )
    except TestCommitRejected as exc:
        return _emit({"error": exc.code, "detail": str(exc)}, 2)
    return _emit({"imported": sha}, 0)


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
    imp = commands.add_parser("import-tests")
    imp.add_argument("--repo-path", required=True)
    imp.add_argument("--bundle", required=True)
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
