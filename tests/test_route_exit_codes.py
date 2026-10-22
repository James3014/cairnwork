"""``cairnwork-route`` exit codes 0, 3 and 4 are distinct at the route level (#182)."""

import json
import subprocess
import sys

import pytest
from test_recovery import project  # noqa: F401 - fixture, used through crashed
from test_recovery_leftovers import _routable, _stuck_dir


@pytest.fixture
def crashed(request):
    return request.getfixturevalue("project")


def route(state, *args):
    done = subprocess.run(
        [sys.executable, "-I", "-m", "cairnwork.route", "--state", str(state), *args],
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    return done.returncode, json.loads(done.stdout)


def test_unknown_task_is_3_leftover_run_dirs_is_4_and_clean_is_0(crashed, tmp_path):
    attempt = _routable(crashed, tmp_path)
    code, out = route(tmp_path, "status", "--task-id", "task-nope")
    assert code == 3 and out["error"] == "UNKNOWN_TASK"
    _stuck_dir(tmp_path, attempt, "q")
    try:
        code, out = route(tmp_path, "recover")
    finally:
        (tmp_path / "runs").chmod(0o700)
    assert code == 4 and out["leftover_run_dirs"] == 2 and "error" not in out
    code, out = route(tmp_path, "recover")  # the leftovers are removable now: clean
    assert code == 0 and out["leftover_run_dirs"] == 0 and "error" not in out
