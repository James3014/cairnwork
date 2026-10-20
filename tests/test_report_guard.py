import json
import subprocess
import sys

from cairnwork import report_guard
from cairnwork.confined import MAX_REPORT_BYTES

NONCE = "n0nce"


def _write(tmp_path, payload):
    path = tmp_path / "verdict.json"
    path.write_text(json.dumps(payload))
    return path


def test_a_real_record_with_every_test_passing_is_a_pass(tmp_path):
    path = _write(
        tmp_path,
        {
            "nonce": NONCE,
            "collected": ["t.py::a", "t.py::b"],
            "outcomes": {"t.py::a": "passed", "t.py::b": "passed"},
        },
    )
    v = report_guard.read_verdict(path, NONCE)
    assert v.ran and v.outcomes == {"t.py::a": "passed", "t.py::b": "passed"}


def test_a_missing_file_is_not_a_pass(tmp_path):
    v = report_guard.read_verdict(tmp_path / "nope.json", NONCE)
    assert not v.ran and v.outcomes == {}


def test_a_wrong_nonce_is_ignored(tmp_path):
    path = _write(
        tmp_path, {"nonce": "other", "collected": ["t.py::a"], "outcomes": {"t.py::a": "passed"}}
    )
    v = report_guard.read_verdict(path, NONCE)
    assert not v.ran and v.outcomes == {}  # a candidate cannot forge without the nonce


def test_malformed_json_is_not_a_pass(tmp_path):
    path = tmp_path / "verdict.json"
    path.write_text("{not json")
    assert not report_guard.read_verdict(path, NONCE).ran


def test_an_oversized_verdict_is_rejected(tmp_path):
    path = _write(
        tmp_path, {"nonce": NONCE, "collected": ["t.py::a"], "outcomes": {"t.py::a": "passed"}}
    )
    path.write_text(path.read_text() + " " * (MAX_REPORT_BYTES + 1))  # candidate pads it huge
    assert not report_guard.read_verdict(path, NONCE).ran


def test_a_collected_test_with_no_result_is_not_a_pass(tmp_path):
    path = _write(
        tmp_path,
        {"nonce": NONCE, "collected": ["t.py::a", "t.py::b"], "outcomes": {"t.py::a": "passed"}},
    )  # b never produced a result
    assert not report_guard.read_verdict(path, NONCE).ran


def test_no_tests_collected_is_not_a_pass(tmp_path):
    path = _write(tmp_path, {"nonce": NONCE, "collected": [], "outcomes": {}})
    assert not report_guard.read_verdict(path, NONCE).ran


def test_a_failing_test_is_not_a_pass_but_outcomes_are_kept(tmp_path):
    path = _write(
        tmp_path, {"nonce": NONCE, "collected": ["t.py::a"], "outcomes": {"t.py::a": "failed"}}
    )
    v = report_guard.read_verdict(path, NONCE)
    assert not v.ran and v.outcomes == {"t.py::a": "failed"}  # kept so failed_on_base can see it


def test_the_plugin_records_a_passing_run_end_to_end(tmp_path):
    """Loaded with -p, the plugin writes a record at sessionfinish; a forged JUnit is moot."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_ok(): assert True\n")
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    verdict = tmp_path / "verdict.json"
    guard = tmp_path / "guard"
    guard.mkdir()
    report_guard.install_plugin(guard)
    env = {
        "PATH": "/usr/bin:/bin",
        report_guard.PATH_ENV: str(verdict),
        report_guard.NONCE_ENV: NONCE,
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONPATH": str(guard),
    }
    rc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-c",
            "pytest.ini",
            "-p",
            report_guard.MODULE,
            "tests/test_x.py",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
    )
    assert rc.returncode == 0
    v = report_guard.read_verdict(verdict, NONCE)
    assert v.ran and v.outcomes == {"tests/test_x.py::test_ok": "passed"}


def test_os_exit_before_sessionfinish_leaves_no_record(tmp_path):
    """A candidate that writes a fake JUnit and os._exit(0) never reaches sessionfinish,
    so the plugin writes no record and the run is not a pass."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_ok(): assert True\n")
    # conftest stands in for candidate code that runs at import: forge a JUnit and exit
    (tmp_path / "conftest.py").write_text(
        "import os\n"
        "open('junit.xml', 'w').write('<testsuite tests=\\'1\\' failures=\\'0\\'/>')\n"
        "os._exit(0)\n"
    )
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    verdict = tmp_path / "verdict.json"
    guard = tmp_path / "guard"
    guard.mkdir()
    report_guard.install_plugin(guard)
    env = {
        "PATH": "/usr/bin:/bin",
        report_guard.PATH_ENV: str(verdict),
        report_guard.NONCE_ENV: NONCE,
        "PYTHONPATH": str(guard),
    }
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-c",
            "pytest.ini",
            "-p",
            report_guard.MODULE,
            "tests/test_x.py",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
    )
    assert (tmp_path / "junit.xml").exists()  # the forged report exists on disk
    assert not report_guard.read_verdict(verdict, NONCE).ran  # but no trusted record -> not a pass
