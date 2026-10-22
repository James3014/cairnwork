"""#117: existing tests run over a candidate next to the deferred tests (p2p.py)."""

import json
from pathlib import Path

import pytest
from conftest import commit_change
from test_deferred_tests import CHECK, REGRESSION, make_test_commit
from test_pipeline import EditWorker

from cairnwork import report_guard
from cairnwork.journal import Journal
from cairnwork.notary import Notary
from cairnwork.ownership import OwnershipLedger
from cairnwork.pipeline import TaskSpec, run_task

pytestmark = pytest.mark.sandbox

CALC = "SLOW = False\ndef add(a, b):\n    return a + b + 1\ndef neg(a):\n    return -a\n"
CONFTEST = (
    "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent.parent / 'src'))\n"
)
SIBLING = "from calc import neg\ndef test_neg():\n    assert neg(2) == -2\n"
SLEEP = "import time\ndef test_slow():\n    time.sleep(60)\n"
FIX_ADD = "SLOW = False\ndef add(a, b):\n    return a + b\ndef neg(a):\n    return -a\n"
FIX_BREAKS_NEG = "SLOW = False\ndef add(a, b):\n    return a + b\ndef neg(a):\n    return a\n"


@pytest.fixture
def project(repo):
    commit_change(
        repo,
        {
            "src/calc.py": CALC,
            "tests/conftest.py": CONFTEST,
            "tests/test_calc.py": "from calc import add\ndef test_add():\n    assert add(2, 3)\n",
            "tests/test_sibling.py": SIBLING,  # a flat layout: no file named for neg()
        },
    )
    return repo


def run(project, tmp_path, worker_files, *, mode, budget=300, extra=None):
    if extra:  # existing tests of the base, not part of the deferred tests
        commit_change(project, extra)
    base, test_commit = make_test_commit(project, {"tests/test_regression.py": REGRESSION})
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    task = TaskSpec("k1", "card", "fix add", "add() is off by one", project, base,
                    ("src/calc.py",), CHECK, test_commit=test_commit,
                    p2p_mode=mode, p2p_budget=budget)  # fmt: skip
    result = run_task(
        task,
        ledger=OwnershipLedger(state / "own.sqlite"),
        notary=Notary(state / "notary.sqlite"),
        journal=Journal(state / "journal.sqlite"),
        worker=EditWorker(worker_files),
        run_root=state / "runs",
    )
    return result


def rule_files(result):
    return [Path(log).parent / "p2p-selection.json" for log in result.logs]


@pytest.mark.parametrize("mode", ["whole-suite", "module-dir"])
def test_a_sibling_regression_in_a_flat_test_directory_is_caught(project, tmp_path, mode):
    result = run(project, tmp_path, {"src/calc.py": FIX_BREAKS_NEG}, mode=mode)
    assert result.status == "FAILED" and not result.verified
    assert result.p2p["applied"] == mode
    assert result.p2p["targets"] >= 1
    # a real test failure is a plain failure: re-run once, fails again, the first run recorded
    assert result.p2p["rerun"]["failed"] == ["tests/test_sibling.py::test_neg"]
    assert result.p2p["rerun"]["mutated"] == [] and result.p2p["rerun"]["survivors"] == 0
    assert result.p2p["p2p_rerun_passed"] is False


def test_the_same_fix_without_the_regression_is_verified_and_the_rule_is_recorded(
    project, tmp_path
):
    result = run(project, tmp_path, {"src/calc.py": FIX_ADD}, mode="whole-suite")
    assert result.status == "COMPLETED", result.reason
    assert result.p2p["applied"] == "whole-suite" and result.p2p["degraded"] == ""
    recorded = [json.loads(p.read_text()) for p in rule_files(result) if p.exists()]
    assert recorded == [result.p2p]  # beside the p2p verifier log (the artifact Core hashed)


def test_the_opt_out_keeps_the_old_behaviour(project, tmp_path):
    result = run(project, tmp_path, {"src/calc.py": FIX_BREAKS_NEG}, mode="off")
    assert result.status == "COMPLETED" and result.p2p["applied"] == "off"


def test_a_base_probe_timeout_degrades_to_module_dir_and_says_so(project, tmp_path):
    result = run(
        project, tmp_path, {"src/calc.py": FIX_BREAKS_NEG}, mode="whole-suite", budget=10,
        extra={"tests/slow/test_slow.py": SLEEP},
    )  # fmt: skip
    # the slow test lives in a nested directory: whole-suite times out on the base, module-dir
    # (the flat tests) does not, and still catches the sibling regression
    assert result.p2p["degraded"] == "base-probe-timeout" and result.p2p["applied"] == "module-dir"
    assert result.status == "FAILED" and not result.verified


def test_a_probe_that_times_out_even_in_module_dir_refuses_the_task(project, tmp_path):
    result = run(
        project, tmp_path, {"src/calc.py": FIX_ADD}, mode="module-dir", budget=5,
        extra={"tests/test_slow.py": SLEEP},
    )  # fmt: skip
    assert result.status == "FAILED" and not result.verified
    assert "P2P_BASE_PROBE_UNUSABLE" in result.reason and "base-probe-timeout" in result.reason


def test_a_candidate_run_that_times_out_is_not_verified(project, tmp_path):
    slow_only_on_candidate = (
        "import time, calc\ndef test_slow():\n    if calc.SLOW:\n        time.sleep(60)\n"
    )
    result = run(
        project, tmp_path, {"src/calc.py": FIX_ADD.replace("SLOW = False", "SLOW = True")},
        mode="whole-suite", budget=10, extra={"tests/test_cslow.py": slow_only_on_candidate},
    )  # fmt: skip
    assert result.p2p["degraded"] == "" and result.p2p["applied"] == "whole-suite"
    assert result.status == "FAILED" and not result.verified


def test_a_degraded_probe_with_no_module_dir_test_refuses_instead_of_verifying(repo, tmp_path):
    # #117 F1: only nested tests that map to no changed file, so module-dir selects nothing;
    # the whole-suite probe times out. That must not end VERIFIED without any P2P run.
    commit_change(
        repo,
        {
            "src/calc.py": CALC,
            "tests/conftest.py": CONFTEST,
            "tests/unit/test_x.py": SIBLING,
            "tests/slow/test_slow.py": SLEEP,
        },
    )
    result = run(repo, tmp_path, {"src/calc.py": FIX_ADD}, mode="whole-suite", budget=10)
    assert result.status == "FAILED" and not result.verified
    assert "P2P_BASE_PROBE_UNUSABLE" in result.reason and "base-probe-timeout" in result.reason


def test_a_run_that_fails_only_because_the_checkout_was_mutated_is_not_re_run(project, tmp_path):
    # F2: every test passes, but the candidate makes a test write into the checkout: that is an
    # integrity failure, not a flaky test, so it blocks without a second try
    writes = (
        "import pathlib, calc\ndef test_writes():\n    if calc.SLOW:\n"
        "        (pathlib.Path(__file__).parent / 'junk.txt').write_text('x')\n"
    )
    result = run(
        project, tmp_path, {"src/calc.py": FIX_ADD.replace("SLOW = False", "SLOW = True")},
        mode="whole-suite", extra={"tests/test_writes.py": writes},
    )  # fmt: skip
    assert result.status == "FAILED" and not result.verified
    assert result.p2p["applied"] == "whole-suite" and "rerun" not in result.p2p


def _fails_and(side_effect):
    # a candidate-only failing test that also does something to the run itself
    head = "import pathlib, subprocess, sys, calc\ndef test_x():\n    if calc.SLOW:\n"
    return head + side_effect + "        assert False\n"


@pytest.mark.parametrize(
    ("side_effect", "recorded"),
    [
        pytest.param(
            "        (pathlib.Path(__file__).parent / 'junk.txt').write_text('x')\n",
            "junk.txt",
            id="checkout-mutated",
        ),
        pytest.param(
            "        subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],\n"
            "                         start_new_session=True)\n",
            "outlived",
            id="surviving-process",
        ),
    ],
)
def test_a_failure_that_also_taints_the_run_is_not_plain_and_not_re_run(
    project, tmp_path, side_effect, recorded
):
    # #130: the first run exits 1 on a failed test, but the run itself is suspect (a mutated
    # checkout, a surviving process): plain_failure stays False, so no second try can wash it
    result = run(
        project, tmp_path, {"src/calc.py": FIX_ADD.replace("SLOW = False", "SLOW = True")},
        mode="whole-suite", extra={"tests/test_taint.py": _fails_and(side_effect)},
    )  # fmt: skip
    assert result.status == "FAILED" and not result.verified
    assert result.p2p["applied"] == "whole-suite" and "rerun" not in result.p2p
    assert recorded in result.reason  # the cause is what the record says, not just FAILED
    assert not list((tmp_path / "state" / "runs").glob("*-r-*"))  # and no second run happened


BIG = "import pytest\n@pytest.mark.parametrize('i', range(100))\ndef test_x(i):\n    assert True\n"


@pytest.mark.parametrize(
    ("big_path", "status", "degraded"),
    [
        ("tests/big/test_big.py", "COMPLETED", "base-probe-report-over-cap"),
        ("tests/test_big.py", "FAILED", None),
    ],
    ids=["nested: degrades to module-dir", "flat: refused, same files"],
)
def test_a_suite_too_large_for_one_report_degrades_or_refuses_and_says_so(
    project, tmp_path, monkeypatch, big_path, status, degraded
):
    # #184: 100 nodes overflow a 1500-byte report cap on the whole-suite probe; the flat tests fit
    monkeypatch.setattr(report_guard, "MAX_REPORT_BYTES", 1500)
    result = run(
        project, tmp_path, {"src/calc.py": FIX_ADD}, mode="whole-suite", extra={big_path: BIG}
    )
    assert result.status == status, result.reason
    if degraded:
        assert result.verified and result.p2p["degraded"] == degraded
        assert result.p2p["applied"] == "module-dir" and result.p2p["targets"] >= 1
    else:
        assert not result.verified and "P2P_BASE_PROBE_UNUSABLE" in result.reason
        assert "base-probe-report-over-cap" in result.reason
        assert "module-dir selects the same files" in result.reason
