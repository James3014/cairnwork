"""#234: UNCOMPARABLE at each boundary. ``cairnwork-verify`` reports status UNCOMPARABLE (exit 2)
and the worker path a FAILED attempt whose reason says UNCOMPARABLE only for an isolated worker;
either way Core is never asked and there is no receipt. The stubbed rows replace only the base and
probe runs; the sandbox tests run everything for real."""

from pathlib import Path

import pytest
from test_deferred_tests import CHECK, REGRESSION, make_test_commit
from test_pipeline import EditWorker
from verify_helpers import FIX, candidate, verify

from cairnwork import p2p, pipeline_judge, verifiers
from cairnwork.core_request import VerifierObservation
from cairnwork.intake import Intake
from cairnwork.journal import Journal
from cairnwork.notary import Notary
from cairnwork.ownership import OwnershipLedger
from cairnwork.pipeline import TaskSpec, run_task
from cairnwork.verify_candidate import verify_candidate
from cairnwork_attest.record import RecordNotFound, read_record

TARGET = "tests/test_regression.py::test_regression"
TEETH = {"rc": 1, "outcomes": {TARGET: "failed"}}
BASE_ROWS = [  # (base run, code): one per row of the mapping table
    pytest.param({"rc": 2, "collect_errors": ("tests/x.py",)}, "BASE_COLLECTION_ERROR", id="rc2"),
    pytest.param({"rc": 2}, "BASE_PYTEST_ERROR", id="rc2-interrupted"),
    pytest.param({"rc": 3}, "BASE_PYTEST_ERROR", id="rc3"),
    pytest.param({"rc": 4}, "BASE_PYTEST_ERROR", id="rc4"),
    pytest.param({"rc": 5}, "BASE_NO_TESTS", id="rc5"),
    pytest.param({"rc": None, "timed_out": True}, "BASE_TIMEOUT", id="timeout"),
    pytest.param({"rc": None}, "BASE_NOT_EXECUTED", id="no-sandbox"),
    pytest.param(TEETH | {"report_too_big": True}, "BASE_REPORT_OVER_CAP", id="over-cap"),
    pytest.param(TEETH | {"recorded": False}, "BASE_NO_REPORT", id="no-record"),
]
PROBE_ROWS = [  # a failed P2P base probe (the base run itself has teeth)
    pytest.param({"rc": None, "timed_out": True}, id="probe-timeout"),
    pytest.param({"rc": 1, "report_too_big": True}, id="probe-over-cap"),
    pytest.param({"rc": 2}, id="probe-rc2"),
    pytest.param({"rc": 1, "recorded": False}, id="probe-no-record"),
]


def _run(spec, run_root, run_id, rc=1, outcomes=None, recorded=True, **flags):
    log = Path(run_root) / run_id / "verifier.log"
    seen = VerifierObservation(spec.verifier_id, "x/verifier.log", "sha256:" + "0" * 64, False)
    outcomes = (outcomes or {}) if recorded else {}  # no record: the guard saw nothing
    return verifiers.VerifierRun(
        spec, rc, flags.get("timed_out", False), (), log, seen, outcomes,
        recorded=recorded, report_too_big=flags.get("report_too_big", False),
        collect_errors=flags.get("collect_errors", ()),
    )  # fmt: skip


@pytest.fixture
def stub(monkeypatch):
    """stub(base=..., probe=...): fake only those runs; record every Core call."""
    calls = []
    monkeypatch.setattr(Notary, "notarize", lambda self, *a, **k: calls.append(a))
    real = verifiers.run_verifiers

    def install(base, probe=None):
        def fake(repo, commit, specs, run_root, run_id, **kw):
            fields = {"-b": base, "-p": probe}.get(run_id[-2:])
            if fields is None:
                return real(repo, commit, specs, run_root, run_id, **kw)
            return tuple(_run(spec, run_root, run_id, **fields) for spec in specs)

        monkeypatch.setattr(pipeline_judge, "run_verifiers", fake)
        monkeypatch.setattr(p2p, "run_verifiers", fake)
        return calls

    return install


def verify_stubbed(project, state):
    base, test_commit = make_test_commit(project, {"tests/test_regression.py": REGRESSION})
    cand = candidate(project, base, {"src/calc.py": FIX})
    out = verify_candidate(
        Intake(state), repo=project, base=base, candidate=cand, allowed_files=("src/calc.py",),
        verifiers=CHECK, test_commit=test_commit,
    )  # fmt: skip
    return out


def no_receipt(notary_path, task_id, attempt_id):
    with pytest.raises(RecordNotFound):
        read_record(notary_path, task_id, attempt_id)
    return True


def assert_uncomparable(out, state, calls, code):
    assert out["status"] == "UNCOMPARABLE" and out["verified"] is False
    assert out["uncomparable"]["code"] == code and out["uncomparable"]["rule"] == "base-compare-v1"
    assert out["reason"].startswith(f"UNCOMPARABLE: {code}: ") and out["core_status"] == ""
    assert calls == [] and no_receipt(state / "notary.sqlite", out["task_id"], out["attempt_id"])


@pytest.mark.parametrize(("base", "code"), BASE_ROWS)
def test_verify_maps_each_base_row_to_uncomparable(project, state, stub, base, code):
    calls = stub(base)
    assert_uncomparable(verify_stubbed(project, state), state, calls, code)


@pytest.mark.parametrize("probe", PROBE_ROWS)
def test_verify_maps_each_p2p_probe_failure_to_uncomparable(project, state, stub, probe):
    calls = stub(TEETH, probe)
    assert_uncomparable(verify_stubbed(project, state), state, calls, "P2P_BASE_PROBE_UNUSABLE")


class IsolatedWorker(EditWorker):
    isolated = True  # as AgyWorker with agy_run_as_user


def run_worker(project, tmp_path, worker):
    base, test_commit = make_test_commit(project, {"tests/test_regression.py": REGRESSION})
    state = tmp_path / "wstate"
    state.mkdir()
    task = TaskSpec("k1", "card", "fix add", "add() is off by one", project, base,
                    ("src/calc.py",), CHECK, test_commit=test_commit,
                    p2p_mode="whole-suite")  # fmt: skip
    result = run_task(
        task, ledger=OwnershipLedger(state / "own.sqlite"), notary=Notary(state / "notary.sqlite"),
        journal=Journal(state / "journal.sqlite"), worker=worker, run_root=state / "runs",
    )  # fmt: skip
    assert no_receipt(state / "notary.sqlite", result.task_id, result.attempt_id)
    phases = [e.phase for e in Journal(state / "journal.sqlite").entries(result.task_id)]
    assert "WORKER_STARTED" in phases and "NOTARIZING" not in phases  # after the worker, no Core
    assert phases[-1] == "FINISHED"  # journal and ledger as for any FAILED attempt
    assert OwnershipLedger(state / "own.sqlite").get(task_id=result.task_id).status == "FAILED"
    return result


ALL_ROWS = [pytest.param(p.values[0], None, p.values[1], id=p.id) for p in BASE_ROWS] + [
    pytest.param(TEETH, p.values[0], "P2P_BASE_PROBE_UNUSABLE", id=p.id) for p in PROBE_ROWS
]


@pytest.mark.parametrize(("base", "probe", "code"), ALL_ROWS)
@pytest.mark.parametrize("isolated", [True, False], ids=["isolated", "same-uid"])
def test_worker_path_says_uncomparable_only_when_the_worker_ran_isolated(
    project, tmp_path, stub, base, probe, code, isolated
):
    calls = stub(base, probe)
    worker = (IsolatedWorker if isolated else EditWorker)({"src/calc.py": FIX})
    result = run_worker(project, tmp_path, worker)
    assert result.status == "FAILED" and not result.verified and calls == []
    prefix = "UNCOMPARABLE: " if isolated else ""
    assert result.reason.startswith(f"{prefix}{code}: "), result.reason


# Real runs (sandbox): the module-level import style, the import-inside-the-test style, a failure
# that only the candidate causes, and tests that already pass on the base.

FIX_DOUBLE = FIX + "def double(a):\n    return 2 * a\n"
AT_MODULE = "from calc import double\ndef test_double():\n    assert double(2) == 4\n"
IN_FUNCTION = "def test_double():\n    from calc import double\n    assert double(2) == 4\n"


def run_cli(project, state, tests, cand_files):
    base, test_commit = make_test_commit(project, {"tests/test_regression.py": tests})
    cand = candidate(project, base, cand_files)
    verifier = " ".join(CHECK[0][1])
    return verify(state, project, base, cand, "--test-commit", test_commit, tests=(verifier,))


@pytest.mark.sandbox
def test_a_fix_introduced_import_at_module_level_is_uncomparable(project, state):
    code, out = run_cli(project, state, AT_MODULE, {"src/calc.py": FIX_DOUBLE})
    assert code == 2 and out["status"] == "UNCOMPARABLE" and out["verified"] is False
    assert out["uncomparable"]["code"] == "BASE_COLLECTION_ERROR"
    assert "tests/test_regression.py" in out["uncomparable"]["detail"]  # B2: the guard's record
    assert "inside the test function" in out["uncomparable"]["detail"]
    assert out["core_status"] == "" and out["binding"] is None


@pytest.mark.sandbox
def test_the_same_import_inside_the_test_function_has_teeth(project, state):
    code, out = run_cli(project, state, IN_FUNCTION, {"src/calc.py": FIX_DOUBLE})
    assert code == 0 and out["status"] == "VERIFIED", out


@pytest.mark.sandbox
def test_a_collection_failure_only_the_candidate_causes_stays_failed(project, state):
    broken = {"src/calc.py": "def plus(a, b):\n    return a + b\n"}  # REGRESSION imports add
    code, out = run_cli(project, state, REGRESSION, broken)
    assert code == 2 and out["status"] == "FAILED" and out["uncomparable"] is None
    assert out["core_status"] not in ("", "VERIFIED")  # judged by Core, not uncomparable


@pytest.mark.sandbox
def test_tests_that_pass_on_the_base_stay_refused(project, state):
    passing = "from calc import add\ndef test_regression():\n    assert add(1, 1) == 3\n"
    code, out = run_cli(project, state, passing, {"src/calc.py": FIX})
    assert code == 2 and out["status"] == "REFUSED" and out["uncomparable"] is None
    assert out["reason"].startswith("TESTS_DO_NOT_FAIL_ON_BASE")


@pytest.mark.sandbox
def test_a_base_run_that_leaves_no_guard_record_is_uncomparable(project, state):
    exits = "import os\ndef test_regression():\n    os._exit(1)\n"
    code, out = run_cli(project, state, exits, {"src/calc.py": FIX})
    assert code == 2 and out["status"] == "UNCOMPARABLE"
    assert out["uncomparable"]["code"] == "BASE_NO_REPORT"


def test_the_agy_worker_is_isolated_only_with_agy_run_as_user():
    from cairnwork.workers.agy import AgyWorker
    from cairnwork.workers.agy_dispatch import Dispatch

    assert AgyWorker().isolated is False
    assert AgyWorker(dispatch=Dispatch("/bin/d", 1, 1, 1)).isolated is False
    assert AgyWorker(dispatch=Dispatch("/bin/d", 1, 1, 1, run_as_user="cw")).isolated is True
