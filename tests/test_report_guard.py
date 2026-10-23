import json
import os
import subprocess
import sys

import pytest

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
    verdict = report_guard.read_verdict(path, NONCE)
    assert not verdict.ran and verdict.outcomes == {}
    assert verdict.too_big  # #184: the caller can say why nothing was read


@pytest.mark.parametrize("nonce", [NONCE, "other"])
def test_a_record_within_the_cap_is_not_too_big_whatever_else_is_wrong(tmp_path, nonce):
    path = _write(
        tmp_path, {"nonce": nonce, "collected": ["t.py::a"], "outcomes": {"t.py::a": "passed"}}
    )
    verdict = report_guard.read_verdict(path, NONCE)
    assert verdict.ran is (nonce == NONCE) and not verdict.too_big


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


@pytest.mark.parametrize("bad", [[["x"]], 123, {"a": 1}, "str", None])
def test_a_malformed_collected_fails_closed_not_crashes(tmp_path, bad):
    """A candidate controls the file; a bad shape must fail closed, never raise (crash)."""
    path = _write(tmp_path, {"nonce": NONCE, "collected": bad, "outcomes": {}})
    assert report_guard.read_verdict(path, NONCE).ran is False


def test_a_non_dict_outcomes_fails_closed(tmp_path):
    path = _write(tmp_path, {"nonce": NONCE, "collected": ["a"], "outcomes": [1, 2]})
    assert report_guard.read_verdict(path, NONCE).ran is False


def test_a_symlinked_verdict_is_not_followed(tmp_path):
    target = tmp_path / "real.json"
    target.write_text(json.dumps({"nonce": NONCE, "collected": ["a"], "outcomes": {"a": "passed"}}))
    link = tmp_path / "verdict.json"
    link.symlink_to(target)
    assert report_guard.read_verdict(link, NONCE).ran is False  # O_NOFOLLOW refuses the symlink


def test_a_non_string_outcome_value_fails_closed(tmp_path):
    path = _write(tmp_path, {"nonce": NONCE, "collected": ["a"], "outcomes": {"a": 123}})
    assert report_guard.read_verdict(path, NONCE).ran is False


def test_a_fifo_in_place_of_the_verdict_does_not_hang(tmp_path):
    """A candidate could delete the verdict and mkfifo in its place; O_NONBLOCK + a regular-file
    check must fail closed instead of blocking the parent forever."""
    path = tmp_path / "verdict.json"
    os.mkfifo(path)
    assert report_guard.read_verdict(path, NONCE).ran is False  # returns, does not hang
