"""Normal drill: a real agent fixes a small bug in a throwaway repo, end to end.

Usage: uv run python scripts/drill_normal.py <empty work dir> [model]

Creates a small project with a failing test, binds a task, lets an Agy model
edit the attempt clone, snapshots and checks the candidate, runs the verifier
on a fresh checkout and notarises with nexus-certify. Prints a JSON result.
The work dir keeps the repository, ledgers and logs for inspection.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from cairnwork.journal import Journal
from cairnwork.notary import Notary
from cairnwork.ownership import OwnershipLedger
from cairnwork.pipeline import TaskSpec, run_task
from cairnwork.verifier_account import VerifierAccount
from cairnwork.workers.agy import AgyWorker

FILES = {
    "src/stats.py": (
        "def average(values):\n"
        '    """Return the arithmetic mean of a non-empty list of numbers."""\n'
        "    return sum(values) / (len(values) + 1)\n"
    ),
    "tests/conftest.py": (
        "import pathlib\nimport sys\n\n"
        "sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / 'src'))\n"
    ),
    "tests/test_stats.py": (
        "from stats import average\n\n\n"
        "def test_average_of_two():\n    assert average([2, 4]) == 3\n\n\n"
        "def test_average_of_one():\n    assert average([5]) == 5\n"
    ),
}

# Run verifiers as the dedicated account when it is set up (Issue #9).
ACCOUNT = VerifierAccount() if VerifierAccount().available() else None


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def make_project(repo: Path) -> str:
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    for name, text in FILES.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=drill", "-c", "user.email=drill@localhost", "commit", "-qm", "base")
    return git(repo, "rev-parse", "HEAD")


def main() -> int:
    work = Path(sys.argv[1]).resolve()
    model = sys.argv[2] if len(sys.argv) > 2 else "gemini-3.8-flash-high"
    if work.exists() and any(work.iterdir()):
        raise SystemExit(f"{work} must be empty")
    repo = work / "project"
    base = make_project(repo)
    task = TaskSpec(
        idempotency_key="drill-normal-1",
        card_hash="drill-card-average",
        title="average() returns the wrong value",
        instructions="average([2, 4]) returns 2.0 but should return 3. Fix average() in "
        "src/stats.py so it returns the arithmetic mean.",
        repo=repo,
        base_commit=base,
        allowed_files=("src/stats.py",),
        verifiers=(("pytest", ("python3", "-m", "pytest", "-q", "tests/test_stats.py")),),
    )
    result = run_task(
        task,
        ledger=OwnershipLedger(work / "ownership.sqlite"),
        notary=Notary(work / "notary.sqlite"),
        journal=Journal(work / "journal.sqlite"),
        worker=AgyWorker(model=model),
        run_root=work / "runs",
        verifier_account=ACCOUNT,
    )
    diff = ""
    if result.candidate_commit:
        diff = git(repo, "diff", base, result.candidate_commit)
    print(
        json.dumps(
            {
                **result.__dict__,
                "model": model,
                "verifier_account": ACCOUNT is not None,
                "diff": diff,
            },
            indent=2,
        )
    )
    return 0 if result.status == "COMPLETED" else 1


if __name__ == "__main__":
    sys.exit(main())
