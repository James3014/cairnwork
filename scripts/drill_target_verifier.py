"""Run one real Nexus-new test file through Cairnwork's verifier, as the verifier account.

Usage: uv run python scripts/drill_target_verifier.py <clean Nexus-new clone> <venv python>

The clone is the one Cairnwork verifies from (not a working tree anyone uses); the
venv comes from scripts/setup_target_venv.sh. Base = the clone's origin/main. Four
candidates are built on top of it with git plumbing (the clone's working tree and
index are not touched) and verified with run_verifiers(account=..., python=...,
base_commit=base):

  pass        a comment added to the module under test: must pass;
  broken      a one-line bug in that module: must fail;
  config      the same bug plus a candidate pytest.ini that deselects the failing
              test: must still fail (only the base commit's settings apply);
  no_venv     the passing candidate with the account's own Python (pytest only):
              must fail, which is why a target needs its own interpreter.

The JSON goes to docs/drills/<date>-nexus-new-verifier.json; ``passed`` is true only
if every case went the expected way.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from cairnwork.verifier_account import VerifierAccount
from cairnwork.verifiers import parse_verifier, run_verifiers

TEST = "tests/services/test_federated_lessons.py"
MODULE = "nexus/services/federated_lessons.py"
GOOD_LINE = 'lesson.get("schema_version") != "lesson_event.v1"'
BAD_LINE = 'lesson.get("schema_version") != "lesson_event.v0"'
FAILING = f"{TEST}::test_validate_and_filter_lessons_rejects_old_schema"
IDENT = {
    "GIT_AUTHOR_NAME": "cairnwork drill",
    "GIT_AUTHOR_EMAIL": "drill@example.invalid",
    "GIT_COMMITTER_NAME": "cairnwork drill",
    "GIT_COMMITTER_EMAIL": "drill@example.invalid",
}


def git(repo: Path, *args: str, env: dict[str, str] | None = None, data: bytes | None = None):
    clean = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=data,
        capture_output=True,
        check=True,
        env={**clean, **(env or {})},
    )
    return done.stdout.decode().strip()


def candidate(repo: Path, base: str, files: dict[str, bytes], message: str) -> str:
    """A commit on top of base with files replaced, made in a private index."""
    with tempfile.TemporaryDirectory() as scratch:
        env = {**IDENT, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        git(repo, "read-tree", base, env=env)
        for path, content in files.items():
            oid = git(repo, "hash-object", "-w", "--stdin", data=content)
            git(repo, "update-index", "--add", "--cacheinfo", f"100644,{oid},{path}", env=env)
        tree = git(repo, "write-tree", env=env)
        return git(repo, "commit-tree", tree, "-p", base, "-m", message, env=env)


def verify(repo: Path, commit: str, base: str, python: str | None, account: VerifierAccount):
    spec = parse_verifier("federated-lessons", ["python3", "-m", "pytest", TEST])
    run = run_verifiers(
        repo,
        commit,
        (spec,),
        account.run_root,
        "drill" + os.urandom(4).hex(),
        account=account,
        python=python,
        base_commit=base,
    )[0]
    private = run.log_path.parent
    log = run.log_path.read_text(errors="replace")
    ini = (private / "pytest.ini").read_text()
    account.remove(private)  # the run root is shared: leave nothing behind
    shutil.rmtree(private, ignore_errors=True)
    summary = [line for line in log.splitlines() if " passed" in line or " failed" in line]
    return {
        "candidate_commit": commit,
        "python": python or account.python,
        "passed": run.observation.passed,
        "returncode": run.returncode,
        "mutated": list(run.mutated),
        "observation_hash": run.observation.artifact_hash,
        "pytest_summary": summary[-1:] if summary else log.splitlines()[-3:],
        "verifier_ini": ini.replace(str(private.parent), "<run root>"),
    }


def main() -> int:
    repo, python = Path(sys.argv[1]).resolve(), sys.argv[2]
    account = VerifierAccount()
    base = git(repo, "rev-parse", "origin/main^{commit}")
    source = git(repo, "show", f"{base}:{MODULE}").encode() + b"\n"
    if source.count(GOOD_LINE.encode()) != 1:
        print(f"{MODULE} at {base} no longer has the line this drill breaks")
        return 1
    broken = source.replace(GOOD_LINE.encode(), BAD_LINE.encode())
    ini = git(repo, "show", f"{base}:pytest.ini").encode() + b"\n"
    deselect = ini.replace(b"addopts = ", f"addopts = --deselect {FAILING} ".encode(), 1)
    commits = {
        "pass": candidate(repo, base, {MODULE: source + b"# drill\n"}, "drill: comment"),
        "broken": candidate(repo, base, {MODULE: broken}, "drill: bug"),
        "config": candidate(
            repo, base, {MODULE: broken, "pytest.ini": deselect}, "drill: bug + candidate ini"
        ),
    }
    expect = {"pass": True, "broken": False, "config": False, "no_venv": False}
    cases = {name: verify(repo, sha, base, python, account) for name, sha in commits.items()}
    cases["no_venv"] = verify(repo, commits["pass"], base, None, account)
    for name, case in cases.items():
        case["expected_pass"] = expect[name]
        print(name, json.dumps(case, sort_keys=True), flush=True)
    result = {
        "date": datetime.date.today().isoformat(),
        "target": "James3014/Nexus-new",
        "base_commit": base,
        "test_file": TEST,
        "verifier_account": account.user,
        "cases": cases,
        "passed": all(cases[name]["passed"] == want for name, want in expect.items()),
        "note": "candidates are drill commits made in Cairnwork's own Nexus-new clone, "
        "not tasks run by an agent; the Core notary is not involved",
    }
    out = Path(__file__).resolve().parent.parent / "docs" / "drills"
    path = out / f"{result['date']}-nexus-new-verifier.json"
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(path, "passed" if result["passed"] else "FAILED")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
