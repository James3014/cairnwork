"""Run one task in a separate process so a test can kill it (crash drills).

Usage: python crash_driver.py <repo> <state dir> <fix text> [orphan]
With 'orphan', the worker starts a detached child tagged CAIRNWORK_ATTEMPT and
sleeps, so the test can SIGKILL this process mid-worker.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

from cairnwork.journal import Journal
from cairnwork.notary import Notary
from cairnwork.ownership import OwnershipLedger
from cairnwork.pipeline import TaskSpec, run_task

repo, state, fix = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
orphan = len(sys.argv) > 4 and sys.argv[4] == "orphan"
base = subprocess.run(
    ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
).stdout.strip()


class Worker:
    def run(self, workdir, _task):
        (workdir / "src" / "calc.py").write_text(fix)
        if orphan:
            late = state / "orphan-wrote"
            code = (
                f"import time, pathlib; time.sleep(4); pathlib.Path({str(late)!r}).write_text('x')"
            )
            subprocess.Popen(
                [sys.executable, "-c", code],
                start_new_session=True,
                env={**os.environ, "CAIRNWORK_ATTEMPT": workdir.name},
            )
            (state / "worker-running").write_text("x")
            time.sleep(60)


task = TaskSpec(
    "k1",
    "card-k1",
    "fix add",
    "add() is off by one",
    repo,
    base,
    ("src/calc.py",),
    (("pytest", ("python3", "-m", "pytest", "-q", "tests/test_calc.py")),),
)
run_task(
    task,
    ledger=OwnershipLedger(state / "own.sqlite"),
    notary=Notary(state / "notary.sqlite"),
    journal=Journal(state / "journal.sqlite"),
    worker=Worker(),
    run_root=state / "runs",
)
