"""Run one task with a P2P step in its own process, so a test can SIGKILL it during that step.

Usage: python p2p_crash_driver.py <repo> <state dir> <base> <test commit> <q|r|n> [account]
The candidate sets calc.SLOW, so the P2P candidate run (-q) sleeps. With 'r' the first P2P run is
replaced by a plain failure, so the re-run (-r) is the one that sleeps. With 'n' nothing sleeps:
the run finishes normally.
"""

import sys
from pathlib import Path

import cairnwork.p2p as p2p
from cairnwork.core_request import VerifierObservation
from cairnwork.journal import Journal
from cairnwork.notary import Notary
from cairnwork.ownership import OwnershipLedger
from cairnwork.pipeline import TaskSpec, run_task
from cairnwork.verifier_account import VerifierAccount
from cairnwork.verifiers import VerifierRun

repo, state, base, tests, step = Path(sys.argv[1]), Path(sys.argv[2]), *sys.argv[3:6]
account = VerifierAccount() if len(sys.argv) > 6 else None
real = p2p._run_once


def once(spec, plan, note, task, judged, run_root, result, verifying, tag):
    if step == "r" and tag == "q":  # a plain failure, so run_plan re-runs (-r)
        observation = VerifierObservation(spec.verifier_id, "q", "q", False)
        failed = {"tests/test_slow.py::test_slow": "failed"}
        return VerifierRun(spec, 1, False, (), run_root / "q.log", observation, failed,
                           plain_failure=True)  # fmt: skip
    return real(spec, plan, note, task, judged, run_root, result, verifying, tag)


p2p._run_once = once


class Worker:
    def run(self, workdir, _task):
        slow = step != "n"
        (workdir / "src" / "calc.py").write_text(
            f"SLOW = {slow}\ndef add(a, b):\n    return a + b\n"
        )


task = TaskSpec("k1", "card", "fix add", "add() is off by one", repo, base, ("src/calc.py",),
                (("pytest", ("python3", "-m", "pytest", "-q", "tests/test_regression.py")),),
                test_commit=tests, p2p_mode="whole-suite", p2p_budget=300)  # fmt: skip
run_task(
    task,
    ledger=OwnershipLedger(state / "own.sqlite"),
    notary=Notary(state / "notary.sqlite"),
    journal=Journal(state / "journal.sqlite"),
    worker=Worker(),
    run_root=state / "runs",
    verifier_account=account,
)
