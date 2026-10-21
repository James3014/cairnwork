"""The ``cairnwork`` command: ``init`` to make a state directory, and ``demo`` to run the
whole governed-verification flow on a self-contained fixture in a few seconds.

This is user-facing orchestration, not part of the governance engine: ``demo`` only calls the
same public ``verify_candidate`` path a real run uses. It builds a tiny throwaway git repo with
a seeded bug and a test that catches it, a ready-made one-line fix as the candidate, and a
private venv, then asks Cairnwork to verify the fix — showing the governed *verification* an
attempt goes through (the isolated clone, the file-scope check, the test run in the sandbox, and
the Core VERIFIED verdict bound to the commit). It does not exercise the task-ownership/dispatch
side of a real run. It needs no GitHub, no AI worker and no API key; setting up the throwaway
venv may fetch pytest from the package cache or index on a cold machine.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from cairnwork.gitcmd import run_git

# The seeded project: total() returns -1 for an empty list instead of 0. The test catches it,
# so it is red on the base; the fix is a one-line change confined to src/calc.py.
_BUGGY = "def total(items):\n    return sum(items) if items else -1\n"
_FIXED = "def total(items):\n    return sum(items)\n"
_CONFTEST = (
    'import pathlib, sys\n\nsys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))\n'
)
_TEST = (
    "from calc import total\n\n\n"
    "def test_known_amounts():\n    assert total([1, 2, 3]) == 6\n\n\n"
    "def test_empty_is_zero():\n    assert total([]) == 0\n"
)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "init":
        return _init(args)
    return _demo(args)


def _init(args) -> int:
    """Create a state directory with a minimal config.json for a real target repository."""
    state = Path(args.dir).resolve()
    if args.verifier_python is not None and not os.path.isabs(args.verifier_python):
        print("error: --verifier-python must be an absolute path", file=sys.stderr)
        return 2
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state, 0o700)  # tighten an existing dir too; Intake.prepare() requires 0700
    config = {"verifier_account": args.verifier_account, "verifier_python": args.verifier_python}
    (state / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    (state / "ACCEPTING").touch(mode=0o600)
    print(f"state ready: {state}")
    print(f"  config.json: {json.dumps(config)}")
    print("  next: cairnwork-issues --state <dir> --repo OWNER/NAME --clone <mirror> run-once")
    return 0


def _demo(args) -> int:
    keep = args.keep
    # Resolve the temp root: on macOS /var/folders is a symlink into /private, and the sandbox
    # denies access when the policy path and the real path differ (AGENTS.md).
    work = Path(tempfile.mkdtemp(prefix="cairnwork-demo.")).resolve()
    try:
        print(f"→ building a throwaway project with a seeded bug in {work}")
        repo, shas = _build_fixture(work / "fixture")
        python = _build_venv(work / "venv")
        state = _demo_state(work / "state", python)
        print("→ asking Cairnwork to verify the fix (isolated clone, file scope, tests, Core)")
        result = _verify(state, repo, shas)
        return _report(result, work if keep else None)
    finally:
        if not keep:
            _rm(work)


def _build_fixture(repo: Path) -> tuple[Path, dict[str, str]]:
    """A git repo whose base has the empty-list bug and a test that catches it (red on the
    base), with the one-line fix as a single commit on the base."""
    repo.mkdir(parents=True)
    _git_init(repo)
    (repo / "src").mkdir()
    (repo / "src" / "calc.py").write_text(_BUGGY)
    (repo / "tests").mkdir()
    (repo / "tests" / "conftest.py").write_text(_CONFTEST)
    (repo / "tests" / "test_calc.py").write_text(_TEST)
    base = _commit_all(repo, "seed: tinyledger total with the empty-list bug")
    run_git(repo, "checkout", "--quiet", "-b", "candidate", base)
    (repo / "src" / "calc.py").write_text(_FIXED)
    candidate = _commit_all(repo, "fix: total([]) == 0")
    run_git(repo, "checkout", "--quiet", base)  # leave HEAD on the base, not the fix
    return repo, {"base": base, "candidate": candidate}


def _build_venv(venv: Path) -> str:
    """A private interpreter with pytest for the verifier to run the target's tests."""
    subprocess.run(["uv", "venv", "--quiet", str(venv)], check=True)  # noqa: S603, S607
    python = venv / "bin" / "python"
    subprocess.run(  # noqa: S603
        ["uv", "pip", "install", "--quiet", "--python", str(python), "pytest"], check=True
    )
    return str(python)


def _demo_state(state: Path, python: str) -> Path:
    state.mkdir(mode=0o700, parents=True)
    config = {"verifier_account": False, "verifier_python": python}
    (state / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    (state / "ACCEPTING").touch(mode=0o600)
    return state


def _verify(state: Path, repo: Path, shas: dict[str, str]) -> dict:
    from cairnwork.intake import Intake
    from cairnwork.verify_candidate import verify_candidate

    intake = Intake(state)
    intake.prepare()
    return verify_candidate(
        intake,
        repo=repo,
        base=shas["base"],
        candidate=shas["candidate"],
        allowed_files=("src/calc.py",),
        verifiers=(("pytest", ("python3", "-m", "pytest", "-q", "tests/test_calc.py")),),
    )


def _report(result: dict, kept: Path | None) -> int:
    # Fail closed: only call it verified when the run says so AND Core recorded VERIFIED.
    verified = bool(result.get("verified")) and result.get("core_status") == "VERIFIED"
    mark = "VERIFIED ✓" if verified else "NOT VERIFIED ✗"
    print(f"\n  verdict:      {mark}")
    print(f"  core status:  {result.get('core_status') or '—'}")
    print(f"  candidate:    {result.get('candidate_commit', '')[:12]}")
    if not verified and result.get("reason"):
        print(f"  reason:       {result['reason']}")
    if kept is not None:
        print(f"  kept the demo tree at {kept}")
    if verified:
        print("\nCairnwork ran the fix in an isolated clone, enforced that it touched only the")
        print("allowed file, ran the tests, and recorded a Core verdict bound to that commit.")
    return 0 if verified else 1


def _git_init(repo: Path) -> None:
    run_git(repo, "init", "--quiet", "-b", "main")
    _identify(repo)


def _identify(repo: Path) -> None:
    run_git(repo, "config", "user.email", "demo@cairnwork.local")
    run_git(repo, "config", "user.name", "Cairnwork Demo")


def _commit_all(repo: Path, message: str) -> str:
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "--quiet", "-m", message)
    return run_git(repo, "rev-parse", "HEAD").strip()


def _rm(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cairnwork", description="Cairnwork quickstart")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="create a state directory for a real target repo")
    init.add_argument("--dir", required=True)
    init.add_argument("--verifier-python", default=None)
    init.add_argument(
        "--verifier-account",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="run verifiers under the dedicated account (default: yes)",
    )
    demo = commands.add_parser("demo", help="run the full flow on a self-contained fixture")
    demo.add_argument("--keep", action="store_true", help="keep the throwaway tree for inspection")
    return parser


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
