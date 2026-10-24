"""#234: base_comparison maps each base run to TEETH, NO_TEETH or UNCOMPARABLE, and the Issue
comment says "no evidence" for an UNCOMPARABLE attempt; B2: the guard's record carries the
collection errors. The verify and worker paths are in test_uncomparable_paths.py."""

# ruff: noqa: F811 (the imported setup fixture is used as a test argument)

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
from test_issue_runner import (
    FakeGitHub,
    issue,
    setup,  # noqa: F401 - pytest fixture, F811 on use
)

import cairnwork.issue_runner as runner
from cairnwork import report_guard
from cairnwork.deferred_tests import Comparison, base_comparison
from cairnwork.intake import Intake
from cairnwork.issue_publish import failure_text, uncomparable_code

TARGET = "tests/test_regression.py::test_regression"
TARGETS = frozenset({TARGET})


def run(name="v", rc=1, *, failed=(TARGET,), **fields):
    """A stand-in VerifierRun with the fields base_comparison reads."""
    return SimpleNamespace(
        spec=SimpleNamespace(verifier_id=name),
        returncode=rc,
        outcomes={node: "failed" for node in failed} | {"tests/test_old.py::test_ok": "passed"},
        timed_out=fields.get("timed_out", False),
        report_too_big=fields.get("report_too_big", False),
        recorded=fields.get("recorded", True),
        collect_errors=fields.get("collect_errors", ()),
    )


# one row per line of the #234 mapping table: (base run, kind, code)
ROWS = [
    pytest.param(run(rc=2, failed=()), "UNCOMPARABLE", "BASE_COLLECTION_ERROR", id="rc2"),
    pytest.param(run(rc=3, failed=()), "UNCOMPARABLE", "BASE_PYTEST_ERROR", id="rc3"),
    pytest.param(run(rc=4, failed=()), "UNCOMPARABLE", "BASE_PYTEST_ERROR", id="rc4"),
    pytest.param(run(rc=5, failed=()), "UNCOMPARABLE", "BASE_NO_TESTS", id="rc5"),
    pytest.param(run(rc=137, failed=()), "UNCOMPARABLE", "BASE_PYTEST_ERROR", id="rc-other"),
    pytest.param(run(rc=None, timed_out=True), "UNCOMPARABLE", "BASE_TIMEOUT", id="timeout"),
    pytest.param(run(rc=None, failed=()), "UNCOMPARABLE", "BASE_NOT_EXECUTED", id="no-sandbox"),
    pytest.param(run(report_too_big=True), "UNCOMPARABLE", "BASE_REPORT_OVER_CAP", id="over-cap"),
    pytest.param(run(recorded=False), "UNCOMPARABLE", "BASE_NO_REPORT", id="no-record"),
    pytest.param(
        run(collect_errors=("tests/test_x.py",)),
        "UNCOMPARABLE",
        "BASE_COLLECTION_ERROR",
        id="guard-collect-error",
    ),  # fmt: skip
    pytest.param(run(rc=0, failed=()), "NO_TEETH", "TESTS_DO_NOT_FAIL_ON_BASE", id="passes"),
    pytest.param(
        run(failed=("tests/test_old.py::test_other",)),
        "NO_TEETH",
        "TESTS_DO_NOT_FAIL_ON_BASE",
        id="only-another-test-fails",
    ),  # fmt: skip
    pytest.param(run(), "TEETH", "", id="target-fails"),
    pytest.param(run(failed=(f"{TARGET}[case-1]",)), "TEETH", "", id="parametrised-target"),
]


@pytest.mark.parametrize(("base", "kind", "code"), ROWS)
def test_each_row_of_the_mapping_table(base, kind, code):
    got = base_comparison([base], TARGETS)
    assert (got.kind, got.code) == (kind, code)


@pytest.mark.parametrize(
    "runs",
    [
        pytest.param(lambda: [run("v1"), run("v2", rc=2, failed=())], id="teeth-then-rc2"),
        pytest.param(lambda: [run("v1", rc=2, failed=()), run("v2")], id="rc2-then-teeth"),
        pytest.param(lambda: [run("v1"), run("v2", recorded=False)], id="teeth-then-no-record"),
    ],
)
def test_mixed_runs_are_uncomparable_even_when_one_has_teeth(runs):
    got = base_comparison(runs(), TARGETS)
    assert got.kind == "UNCOMPARABLE"
    assert ("v1" in got.detail) != ("v2" in got.detail)  # names the run that could not compare


def test_every_run_is_judged_and_the_detail_keeps_the_evidence():
    got = base_comparison([run("v1", rc=None, timed_out=True), run("v2", rc=5, failed=())], TARGETS)
    assert got.code == "BASE_TIMEOUT"  # the first problem names the code
    assert "v1: BASE_TIMEOUT" in got.detail and "v2: BASE_NO_TESTS (exit 5)" in got.detail


def test_a_collection_error_in_a_deferred_file_tells_the_author_where_to_import():
    own = run(rc=2, failed=(), collect_errors=("tests/test_regression.py",))
    other = run(rc=2, failed=(), collect_errors=("tests/test_env.py",))
    assert "inside the test function" in base_comparison([own], TARGETS).detail
    assert "inside the test function" not in base_comparison([other], TARGETS).detail
    assert "tests/test_env.py" in base_comparison([other], TARGETS).detail


def test_the_worker_reason_says_uncomparable_only_for_an_isolated_worker():
    found = Comparison("UNCOMPARABLE", "BASE_TIMEOUT", "v: BASE_TIMEOUT")
    assert found.worker_reason(isolated=True) == "UNCOMPARABLE: BASE_TIMEOUT: v: BASE_TIMEOUT"
    assert found.worker_reason(isolated=False) == "BASE_TIMEOUT: v: BASE_TIMEOUT"
    assert found.worker_reason(isolated="yes") == "BASE_TIMEOUT: v: BASE_TIMEOUT"  # only True


def _intake(tmp_path, isolated=True):
    return SimpleNamespace(
        config=lambda: {"agy_run_as_user": "cw-worker" if isolated else None}, run_root=tmp_path
    )


def _runner_log(tmp_path, task_id, reason, status="FAILED"):
    out = {"task_id": task_id, "status": status, "reason": reason, "verified": False}
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / f"{task_id}.runner.log").write_text("git noise\n" + json.dumps(out) + "\n")


def test_the_uncomparable_code_comes_from_the_runner_output_of_an_isolated_worker(tmp_path):
    _runner_log(tmp_path, "t1", "UNCOMPARABLE: BASE_NO_TESTS: v: BASE_NO_TESTS (exit 5)")
    assert uncomparable_code(_intake(tmp_path), "t1") == "BASE_NO_TESTS"
    assert uncomparable_code(_intake(tmp_path, isolated=False), "t1") == ""  # same uid: forgeable
    assert uncomparable_code(_intake(tmp_path), "t2") == ""  # no log
    _runner_log(tmp_path, "t3", "BASE_NO_TESTS: v: BASE_NO_TESTS (exit 5)")  # plain FAILED
    assert uncomparable_code(_intake(tmp_path), "t3") == ""
    _runner_log(tmp_path, "t4", "UNCOMPARABLE: BASE_NO_TESTS: x", status="COMPLETED")
    assert uncomparable_code(_intake(tmp_path), "t4") == ""
    _runner_log(tmp_path, "t5", "UNCOMPARABLE: `x` @someone: y")  # not a code: not echoed
    assert uncomparable_code(_intake(tmp_path), "t5") == ""
    assert uncomparable_code(_intake(tmp_path), "../t1") == ""


def test_the_issue_comment_says_no_evidence_and_only_the_code():
    failed = {"status": "FAILED", "phase": "FINISHED"}
    text = failure_text(failed, "BASE_COLLECTION_ERROR")
    assert "沒有證據（`UNCOMPARABLE: BASE_COLLECTION_ERROR`）" in text and "沒有完成" not in text
    assert "沒有完成" in failure_text(failed) and "UNCOMPARABLE" not in failure_text(failed)
    assert "UNCOMPARABLE" not in failure_text({"status": "COMPLETED"}, "BASE_TIMEOUT")
    assert "@x" not in failure_text(failed, "@x") and "沒有完成" in failure_text(failed, "@x")


@pytest.mark.parametrize("isolated", [True, False])
def test_settle_comments_no_evidence_and_relabels_as_failed(setup, monkeypatch, isolated):
    intake, github = setup["intake"], FakeGitHub([issue()])
    taken = runner.take(intake, github, setup["clone"])[0]
    task_id = taken["task_id"]
    _runner_log(intake.run_root, task_id, "UNCOMPARABLE: BASE_TIMEOUT: v: BASE_TIMEOUT (exit None)")
    status = {"task_id": task_id, "status": "FAILED", "core_verified": False, "phase": "FINISHED"}
    monkeypatch.setattr(Intake, "status", lambda self, **kw: status)
    config = intake.config() | {"agy_run_as_user": "cw-worker" if isolated else None}
    monkeypatch.setattr(Intake, "config", lambda self: config)
    runner.settle(intake, github, setup["clone"])
    text = github.comments[-1][1]
    assert github.labels[7] == {runner.FAILED_LABEL}
    assert ("`UNCOMPARABLE: BASE_TIMEOUT`" in text) is isolated
    assert "v: BASE_TIMEOUT" not in text and "exit None" not in text  # never the detail


# B2 (#234): the guard records whether its record exists and which collectors failed

NONCE = "n0nce"


def _record(tmp_path, payload):
    path = tmp_path / "verdict.json"
    path.write_text(json.dumps(payload))
    return path


def test_the_record_says_it_was_there_and_which_collectors_failed(tmp_path):
    # #234 B2: base_comparison needs "a record exists" and the collection errors, not just ran
    record = {"nonce": NONCE, "collected": ["t.py::a"], "outcomes": {"t.py::a": "failed"}}
    path = _record(tmp_path, record | {"collect_errors": ["tests/test_y.py"]})
    v = report_guard.read_verdict(path, NONCE)
    assert v.recorded and v.collect_errors == ("tests/test_y.py",) and not v.ran
    assert report_guard.read_verdict(_record(tmp_path, record), NONCE).collect_errors == ()
    assert not report_guard.read_verdict(tmp_path / "nope.json", NONCE).recorded
    assert not report_guard.read_verdict(_record(tmp_path, record | {"nonce": "x"}), NONCE).recorded


@pytest.mark.parametrize("bad", [{"x": 1}, [1], "tests/test_y.py"])
def test_malformed_collect_errors_mean_no_record(tmp_path, bad):
    path = _record(
        tmp_path,
        {"nonce": NONCE, "collected": ["t.py::a"], "outcomes": {"t.py::a": "passed"},
         "collect_errors": bad},
    )  # fmt: skip
    v = report_guard.read_verdict(path, NONCE)
    assert not v.recorded and not v.ran and v.outcomes == {}


def test_the_plugin_records_a_collection_error_end_to_end(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("import no_such_module\ndef test_ok(): pass\n")
    (tmp_path / "tests" / "test_y.py").write_text("def test_ok(): assert True\n")
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    verdict, guard = tmp_path / "verdict.json", tmp_path / "guard"
    guard.mkdir()
    report_guard.install_plugin(guard)
    env = {
        "PATH": "/usr/bin:/bin", report_guard.PATH_ENV: str(verdict),
        report_guard.NONCE_ENV: NONCE, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONPATH": str(guard),
    }  # fmt: skip
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", "pytest.ini",
         "-p", report_guard.MODULE, "tests"],
        cwd=tmp_path, env=env, capture_output=True, check=False,
    )  # fmt: skip
    assert done.returncode == 2  # pytest stops on a collection error
    v = report_guard.read_verdict(verdict, NONCE)
    assert v.recorded and v.collect_errors == ("tests/test_x.py",)
