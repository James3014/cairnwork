"""The Gateway-facing route (council F), end to end with a fake agy and the real Core.

The runner is a separate process; these tests drive the ``cairnwork-route``
command the way the Gateway would.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import commit_change, git

pytestmark = pytest.mark.sandbox

FIX = "def add(a, b):\n    return a + b\n"


@pytest.fixture
def project(repo):
    commit_change(
        repo,
        {
            "src/calc.py": "def add(a, b):\n    return a + b + 1\n",
            "tests/conftest.py": (
                "import sys, pathlib\n"
                "sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / 'src'))\n"
            ),
            "tests/test_calc.py": (
                "from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n"
            ),
        },
    )
    return repo


@pytest.fixture
def state(tmp_path):
    """A state dir whose worker is a fake agy that writes the fix after `delay` seconds."""
    agy = tmp_path / "fake-agy"
    delay = tmp_path / "delay"
    delay.write_text("0")
    (tmp_path / "fix").write_text(FIX)
    agy.write_text(
        f"#!{sys.executable}\nimport pathlib, time\n"
        f"time.sleep(float(open({str(delay)!r}).read()))\n"
        f"pathlib.Path('src/calc.py').write_text(open({str(tmp_path / 'fix')!r}).read())\n"
    )
    agy.chmod(0o755)
    path = tmp_path / "state"
    path.mkdir(mode=0o700)
    (path / "config.json").write_text(json.dumps({"agy": str(agy), "verifier_account": False}))
    return path


def route(state, *args, stdin=None, cwd=None):
    done = subprocess.run(
        [sys.executable, "-I", "-m", "cairnwork.route", "--state", str(state), *args],
        input=json.dumps(stdin) if stdin is not None else "",
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=cwd,
    )
    return done.returncode, json.loads(done.stdout)


def request(project, key="gw-1"):
    return {
        "idempotency_key": key,
        "card_hash": "card-" + key,
        "title": "fix add",
        "instructions": "add() is off by one",
        "repo": str(project),
        "base_commit": git(project, "rev-parse", "HEAD"),
        "allowed_files": ["src/calc.py"],
        "verifiers": [["pytest", ["python3", "-m", "pytest", "-q", "tests/test_calc.py"]]],
    }


def wait_finished(state, task_id, limit=120):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        _, status = route(state, "status", "--task-id", task_id)
        if status["status"] not in ("BOUND", "DISPATCHED"):
            return status
        time.sleep(0.5)
    raise AssertionError(f"{task_id} still running")


def test_drained_by_default_and_unknown_tasks_are_reported(state, project):
    assert route(state, "submit", stdin=request(project)) == (
        2,
        {"error": "NOT_ACCEPTING", "detail": "Cairnwork takes no new tasks"},
    )
    code, out = route(state, "status", "--task-id", "task-nope")
    assert code == 3 and out["error"] == "UNKNOWN_TASK"
    assert route(state, "in-flight")[1] == {"accepting": False, "count": 0, "task_ids": []}


def test_submitted_task_is_fixed_and_core_verified(state, project):
    route(state, "accept")
    code, out = route(state, "submit", stdin=request(project))
    assert code == 0 and out["duplicate"] is False
    status = wait_finished(state, out["task_id"])
    assert status["status"] == "COMPLETED" and status["core_verified"] is True
    assert status["owner"] == "cairnwork" and status["candidate_commit"]
    # a resend returns the same task and starts nothing, also after a rollback (drain)
    route(state, "drain")
    code, again = route(state, "submit", stdin=request(project))
    assert code == 0 and again == {**out, "status": "COMPLETED", "duplicate": True}
    assert route(state, "status", "--key", "gw-1")[1]["task_id"] == out["task_id"]
    other = {**request(project), "title": "something else"}
    code, clash = route(state, "submit", stdin=other)
    assert code == 2 and clash["error"] == "KeyReusedWithDifferentRequest"
    code, refused = route(state, "cancel", "--task-id", out["task_id"], "--owner", "cairnwork")
    assert code == 2 and refused["error"] == "NOT_CANCELLABLE"
    code, refused = route(state, "cancel", "--task-id", out["task_id"], "--owner", "nexus-new")
    assert code == 2 and refused["error"] == "OWNER_MISMATCH"


def test_a_wrong_fix_has_a_candidate_but_is_not_core_verified(state, project):
    (state.parent / "fix").write_text("def add(a, b):\n    return a - b\n")
    route(state, "accept")
    _, out = route(state, "submit", stdin=request(project))
    status = wait_finished(state, out["task_id"])
    assert status["candidate_commit"] and status["core_verified"] is False
    assert status["status"] == "FAILED"


def test_recovery_cannot_settle_a_task_its_runner_still_holds(state, project):
    (state.parent / "delay").write_text("4")
    route(state, "accept")
    _, out = route(state, "submit", stdin=request(project))
    code, busy = route(state, "recover")  # right after submit: the runner holds the lock
    assert code == 2 and busy["error"] == "RUN_LOCK_BUSY"
    assert route(state, "in-flight")[1]["count"] == 1
    assert wait_finished(state, out["task_id"])["status"] == "COMPLETED"
    assert route(state, "in-flight")[1]["count"] == 0


def test_a_killed_runner_leaves_a_failed_task_never_a_rerun(state, project):
    (state.parent / "delay").write_text("30")
    route(state, "accept")
    _, out = route(state, "submit", stdin=request(project))
    runner = _runner_pid(out["task_id"])
    os.killpg(runner, signal.SIGKILL)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        code, settled = route(state, "recover")
        if code == 0:
            break
        time.sleep(0.5)
    assert code == 0, settled
    assert route(state, "status", "--task-id", out["task_id"])[1]["status"] == "FAILED"
    code, again = route(state, "submit", stdin=request(project))
    assert again["status"] == "FAILED" and again["duplicate"] is True


def test_requests_outside_the_schema_are_refused(state, project):
    route(state, "accept")
    code, out = route(state, "submit", stdin={**request(project), "extra": 1})
    assert code == 2 and out["error"] == "BAD_REQUEST"
    code, out = route(state, "submit", stdin={**request(project), "repo": "relative/path"})
    assert code == 2 and out["error"] == "BAD_REQUEST"
    code, out = route(state, "submit", stdin={**request(project), "allowed_files": ["../x"]})
    assert code == 2 and out["error"] == "BAD_SCOPE"


def _runner_pid(task_id):
    listing = subprocess.run(
        ["/bin/ps", "-axww", "-o", "pid=,command="], capture_output=True, text=True, check=True
    ).stdout
    for line in listing.splitlines():
        if "cairnwork.route" in line and f"--task-id {task_id}" in line:
            return int(line.split()[0])
    raise AssertionError("runner not found")


def test_shadow_check_validates_without_side_effects(state, project):
    assert route(state, "check", stdin=request(project)) == (0, {"would_accept": False})
    code, out = route(state, "check", stdin={**request(project), "allowed_files": ["../x"]})
    assert code == 2 and out["error"] == "BAD_SCOPE"
    assert sorted(p.name for p in state.iterdir()) == ["config.json"]


def _with_mirror(state, mirror):
    config = json.loads((state / "config.json").read_text())
    (state / "config.json").write_text(json.dumps({**config, "mirror": str(mirror)}))


def test_a_mirror_keeps_every_write_out_of_the_callers_repository(state, project, tmp_path):
    mirror = tmp_path / "mirror"
    git(tmp_path, "clone", "-q", str(project), str(mirror))  # Cairnwork's own clone
    _with_mirror(state, mirror)
    refs_before = git(project, "for-each-ref")
    route(state, "drain")
    code, out = route(state, "submit", stdin=request(project))
    assert code == 2 and out["error"] == "NOT_ACCEPTING"
    assert "cairnwork-bases" not in git(mirror, "for-each-ref")  # drained: nothing fetched
    route(state, "accept")
    code, out = route(state, "submit", stdin=request(project))
    assert code == 0, out
    status = wait_finished(state, out["task_id"])
    assert status["status"] == "COMPLETED" and status["core_verified"] is True
    candidate = status["candidate_commit"]
    assert git(mirror, "cat-file", "-t", candidate) == "commit"
    base = request(project)["base_commit"]
    assert git(mirror, "rev-parse", f"refs/cairnwork-bases/{base}") == base  # pinned
    assert git(project, "for-each-ref") == refs_before  # no ref written in the caller's repo
    probe = subprocess.run(
        ["git", "-C", str(project), "cat-file", "-e", candidate], capture_output=True, check=False
    )
    assert probe.returncode != 0  # nor the candidate's objects
    code, again = route(state, "submit", stdin=request(project))  # resend: no new fetch needed
    assert code == 0 and again["duplicate"] is True and again["task_id"] == out["task_id"]
    bad = {**request(project, key="gw-2"), "base_commit": "d" * 40}
    code, refused = route(state, "submit", stdin=bad)
    assert code == 2 and refused["error"] == "BAD_REQUEST"


def test_a_missing_mirror_is_a_config_error(state, project, tmp_path):
    _with_mirror(state, tmp_path / "nowhere")
    route(state, "accept")
    code, out = route(state, "submit", stdin=request(project))
    assert code == 2 and out["error"] == "BAD_CONFIG"


def test_a_request_from_the_mirror_itself_is_not_fetched_again(state, project):
    _with_mirror(state, project)  # e.g. the Issue runner's clone is the mirror
    route(state, "accept")
    code, out = route(state, "submit", stdin=request(project))
    assert code == 0, out
    assert "cairnwork-bases" not in git(project, "for-each-ref")
    assert wait_finished(state, out["task_id"])["status"] == "COMPLETED"


def test_another_submit_pinning_its_base_does_not_upset_a_running_attempt(state, project, tmp_path):
    mirror = tmp_path / "mirror"
    git(tmp_path, "clone", "-q", str(project), str(mirror))
    _with_mirror(state, mirror)
    (state.parent / "delay").write_text("4")
    route(state, "accept")
    _, first = route(state, "submit", stdin=request(project))
    deadline = time.monotonic() + 30
    while route(state, "status", "--task-id", first["task_id"])[1]["status"] != "DISPATCHED":
        assert time.monotonic() < deadline
        time.sleep(0.2)
    commit_change(project, {"README.md": "a newer base\n"})
    code, second = route(state, "submit", stdin=request(project, key="gw-2"))
    assert code == 0, second
    assert wait_finished(state, first["task_id"])["status"] == "COMPLETED"


def test_the_runner_ignores_the_callers_working_tree(state, project, tmp_path):
    """The Gateway calls cairnwork-route from its own tree, which has a `product`
    package of its own; the runner must not import it (2026-10-20 route witness)."""
    caller = tmp_path / "caller-tree"
    (caller / "product").mkdir(parents=True)
    (caller / "product" / "__init__.py").write_text("raise ImportError('caller tree shadowed')\n")
    route(state, "accept")  # intake is closed by default
    script = Path(sys.executable).parent / "cairnwork-route"  # what the Gateway runs
    done = subprocess.run(
        [str(script), "--state", str(state), "submit"],
        input=json.dumps(request(project)), capture_output=True, text=True, timeout=120,
        check=False, cwd=caller,
    )  # fmt: skip
    assert done.returncode == 0, done.stdout + done.stderr
    out = json.loads(done.stdout)
    status = wait_finished(state, out["task_id"])
    # COMPLETED needs a Core verdict: the runner imported nexus-certify's real `product`
    assert status["status"] == "COMPLETED" and status["core_verified"] is True
