"""#234: base_comparison maps each base run to TEETH, NO_TEETH or UNCOMPARABLE; B2: the guard's
record carries the collection errors. The verify and worker paths are in
test_uncomparable_paths.py."""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from cairnwork import report_guard
from cairnwork.deferred_tests import Comparison, base_comparison

TARGET = "tests/test_regression.py::test_regression"
TARGETS = frozenset({TARGET})


def run(name: str = "v", rc: int | None = 1, *, failed=(TARGET,), **fields):
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
    pytest.param(
        run(rc=2, failed=(), collect_errors=("tests/test_x.py",)),
        "UNCOMPARABLE",
        "BASE_COLLECTION_ERROR",
        id="rc2-collect-error",
    ),
    pytest.param(run(rc=2, failed=()), "UNCOMPARABLE", "BASE_PYTEST_ERROR", id="rc2-interrupted"),
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
    ),
    pytest.param(run(rc=0, failed=()), "NO_TEETH", "TESTS_DO_NOT_FAIL_ON_BASE", id="passes"),
    pytest.param(
        run(failed=("tests/test_old.py::test_other",)),
        "NO_TEETH",
        "TESTS_DO_NOT_FAIL_ON_BASE",
        id="only-another-test-fails",
    ),
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
    truthy: object = "yes"  # only True counts, not any truthy value
    assert found.worker_reason(isolated=truthy) == "BASE_TIMEOUT: v: BASE_TIMEOUT"  # pyright: ignore[reportArgumentType]


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
