"""Time ``cairnwork-verify`` with deferred tests under each p2p_selection mode (#117).

    python scripts/measure_p2p.py REPO SRC_FILE VERIFIER_PYTHON [--tests-root tests]

Clones REPO (any local git repository with a pytest suite under the tests root) into a scratch
directory, makes a candidate that appends a marker to SRC_FILE and a deferred test commit that
checks the marker, and runs the verifier once per mode, one fresh test commit each (a test commit
is single-use). Prints one JSON line per mode: status, seconds, the recorded p2p rule. Needs
macOS Seatbelt (or bwrap) and ``pytest`` in VERIFIER_PYTHON.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def prepare(args) -> tuple[Path, Path, Path, str, str]:
    root = Path(tempfile.mkdtemp(prefix="measure-p2p-"))
    project, origin = root / "project", root / "origin.git"
    git(root, "clone", "-q", "--no-hardlinks", args.repo, str(project))
    git(project, "checkout", "-q", "--detach", "HEAD")
    git(root, "clone", "-q", "--bare", str(project), str(origin))
    git(project, "remote", "set-url", "origin", str(origin))
    base = git(project, "rev-parse", "HEAD")
    git(project, "config", "user.email", "m@e")
    git(project, "config", "user.name", "m")
    path = project / args.src_file
    path.write_text(path.read_text() + "\nCAIRNWORK_MEASURE = 1\n")
    git(project, "commit", "-q", "-am", "candidate")
    cand = git(project, "rev-parse", "HEAD")
    git(project, "branch", "cand", cand)
    git(project, "checkout", "-q", "--detach", base)
    return root, project, origin, base, cand


def push_test_commit(root: Path, origin: Path, base: str, args, mode: str) -> str:
    module = args.src_file[:-3].replace("/", ".")
    work = root / f"author-{mode}"
    git(root, "clone", "-q", str(origin), str(work))
    git(work, "config", "user.email", "m@e")
    git(work, "config", "user.name", "m")
    git(work, "checkout", "-q", "--detach", base)
    test = work / args.tests_root / "test_zz_measure.py"
    test.write_text(
        "import importlib\n\n\ndef test_marker():\n"
        f"    assert importlib.import_module('{module}').CAIRNWORK_MEASURE == 1  # {mode}\n"
    )
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "tests")
    commit = git(work, "rev-parse", "HEAD")
    git(work, "push", "-q", "origin", f"{commit}:refs/heads/t-{mode}")
    return commit


def run_mode(args, ctx, mode: str) -> dict:
    root, project, origin, base, cand = ctx
    state = root / f"state-{mode}"
    state.mkdir(mode=0o700)
    (state / "ACCEPTING").touch()
    config = {"verifier_account": False, "verifier_python": args.verifier_python,
              "p2p_selection": mode, "test_roots": [args.tests_root]}  # fmt: skip
    (state / "config.json").write_text(json.dumps(config))
    commit = push_test_commit(root, origin, base, args, mode)
    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, "-I", "-m", "cairnwork.verify_cli", "--state", str(state), "run",
         "--repo-path", str(project), "--base", base, "--candidate", cand,
         "--allowed-file", args.src_file, "--test-commit", commit,
         "--verifier", f"python3 -m pytest -q {args.tests_root}/test_zz_measure.py"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    seconds = round(time.monotonic() - started, 1)
    try:
        out = json.loads(done.stdout)
    except ValueError:
        out = {"status": "ERROR", "reason": (done.stdout + done.stderr)[-300:]}
    keys = ("status", "reason", "p2p")
    return {"mode": mode, "seconds": seconds} | {key: out.get(key) for key in keys}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("src_file")
    ap.add_argument("verifier_python")
    ap.add_argument("--tests-root", default="tests")
    ap.add_argument("--modes", default="off,module-dir,whole-suite")
    args = ap.parse_args()
    ctx = prepare(args)
    for mode in args.modes.split(","):
        print(json.dumps(run_mode(args, ctx, mode)), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
