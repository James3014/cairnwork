"""The Agy worker's process boundary, with a fake agy binary (no model is called)."""

import json
import os
import subprocess
import sys
import time
import uuid
from types import SimpleNamespace

import pytest

from cairnwork.confined import ATTEMPT_ENV, MAX_LOG_BYTES
from cairnwork.workers import agy as agy_module
from cairnwork.workers.agy import AgyWorker, AgyWorkerError

# The worker's sweep kills every process on the host marked with the attempt name, so names
# shared with another test run (parallel agents) would kill this run's fake agy: make them unique.
RUN = uuid.uuid4().hex[:8]


def attempt(n):
    return f"attempt{n}-{RUN}"


TASK = SimpleNamespace(title="t", instructions="i", allowed_files=("a.py",))


def fake_agy(tmp_path, body):
    path = tmp_path / "agy"
    path.write_text(f"#!{sys.executable}\nimport json, os, sys\n{body}\n")
    path.chmod(0o755)
    return str(path)


def expire_once_ready(monkeypatch, ready, only=None):
    """Make the worker's timeout fire only after the fake binary wrote `ready`, so a loaded
    host cannot kill it (or time it out) before it has set up what the test inspects.
    With `only`, just the process started with that path expires; others wait normally."""
    real = subprocess.Popen

    class Popen(real):
        def wait(self, timeout=None):
            mine = only is None or (isinstance(self.args, list) and self.args[0] == only)
            if timeout is not None and mine:
                deadline = time.monotonic() + 120
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                timeout = 0.2
            return super().wait(timeout=timeout)

    monkeypatch.setattr(agy_module.subprocess, "Popen", Popen)


def test_agy_gets_only_the_environment_it_needs(tmp_path, monkeypatch):
    monkeypatch.setenv("CW_FAKE_TOKEN", "secret-value")
    dump = tmp_path / "env.json"
    agy = fake_agy(tmp_path, f"open({str(dump)!r}, 'w').write(json.dumps(dict(os.environ)))")
    workdir = tmp_path / attempt(1)
    workdir.mkdir()
    AgyWorker(agy=agy).run(workdir, TASK)
    env = json.loads(dump.read_text())
    assert "CW_FAKE_TOKEN" not in env
    assert env[ATTEMPT_ENV] == attempt(1)
    assert env["HOME"] == os.environ["HOME"]


def test_agy_log_is_capped(tmp_path):
    agy = fake_agy(tmp_path, f"sys.stdout.write('x' * {MAX_LOG_BYTES * 2})")
    workdir = tmp_path / attempt(2)
    workdir.mkdir()
    AgyWorker(agy=agy).run(workdir, TASK)
    assert (tmp_path / f"{attempt(2)}.agy.log").stat().st_size == MAX_LOG_BYTES + 1


def test_a_marked_process_agy_leaves_behind_is_killed(tmp_path):
    agy = fake_agy(
        tmp_path,
        "import subprocess\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
        " start_new_session=True)\n"
        "print('started', flush=True)",
    )
    workdir = tmp_path / attempt(3)
    workdir.mkdir()
    started = time.monotonic()
    AgyWorker(agy=agy).run(workdir, TASK)
    assert time.monotonic() - started < 30


def test_an_unmarked_process_holding_the_output_fails_the_run(tmp_path):
    agy = fake_agy(
        tmp_path,
        "import subprocess\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(15)'],"
        " start_new_session=True, env={})\n"
        "print('started', flush=True)",
    )
    workdir = tmp_path / attempt(4)
    workdir.mkdir()
    started = time.monotonic()
    with pytest.raises(AgyWorkerError, match="holds its output"):
        AgyWorker(agy=agy).run(workdir, TASK)
    assert time.monotonic() - started < 30


def _prompt(tmp_path, task):
    seen = tmp_path / "argv.json"
    agy = fake_agy(tmp_path, f"open({str(seen)!r}, 'w').write(json.dumps(sys.argv))")
    workdir = tmp_path / attempt(1)
    workdir.mkdir()
    AgyWorker(agy=agy).run(workdir, task)
    argv = json.loads(seen.read_text())
    return argv[argv.index("-p") + 1]


def test_the_prompt_asks_agy_to_work_alone(tmp_path):
    """nexus-learning#114 pilot: agy started a researcher subagent and waited for it until
    the print timeout, twice, changing nothing; told to work alone it edited the file."""
    assert "do not start subagents" in _prompt(tmp_path, TASK)


@pytest.mark.parametrize(("test_commit", "said"), [("a" * 40, True), ("", False)])
def test_hidden_tests_are_mentioned_only_when_the_task_has_them(tmp_path, test_commit, said):
    """Without a test_commit the judging tests are the repository's own: reading them helps."""
    task = SimpleNamespace(**vars(TASK), test_commit=test_commit)
    assert ("not in this repository; do not look for" in _prompt(tmp_path, task)) is said


# --- account-pool dispatcher (state config agy_dispatch) ---


def fake_dispatcher(tmp_path, body):
    path = tmp_path / "pool-dispatch"
    path.write_text(f"#!{sys.executable}\nimport json, os, sys\n{body}\n")
    path.chmod(0o755)
    return str(path)


def _dispatched(tmp_path, body, task=TASK, **settings):
    from cairnwork.workers.agy_dispatch import Dispatch

    path = fake_dispatcher(tmp_path, body)
    workdir = tmp_path / attempt(1)
    workdir.mkdir()
    worker = AgyWorker(model="m-1", timeout_seconds=900, dispatch=Dispatch(path, **settings))
    return worker, workdir, path


def test_the_dispatcher_gets_a_pinned_argv_and_the_prompt_on_stdin(tmp_path):
    seen = tmp_path / "seen.json"
    body = f"open({str(seen)!r}, 'w').write(json.dumps([sys.argv, sys.stdin.read(), os.getcwd()]))"
    task = SimpleNamespace(title="t", instructions="i", allowed_files=("b/z.py", "a.py"))
    worker, workdir, path = _dispatched(tmp_path, body, task)
    worker.run(workdir, task)
    argv, stdin, cwd = json.loads(seen.read_text())
    assert argv == [
        path,
        f"--cwd={workdir}",
        "--mode=accept-edits",
        "--model=m-1",
        "--timeout=900",
        "--pool-wait-timeout=600",
        "--max-calls=1",
        "--provider-stall-seconds=900",
        "--stream-no-progress-seconds=900",
        "--write-path=a.py",
        "--write-path=b/z.py",
    ]
    assert "Task: t" in stdin and "a.py, b/z.py" in stdin
    assert cwd == str(workdir.resolve())


def test_dispatcher_timing_settings_reach_its_argv(tmp_path):
    seen = tmp_path / "seen.json"
    worker, workdir, _ = _dispatched(
        tmp_path,
        f"open({str(seen)!r}, 'w').write(json.dumps(sys.argv))",
        pool_wait_seconds=11,
        provider_stall_seconds=22,
        stream_no_progress_seconds=33,
    )
    worker.run(workdir, TASK)
    argv = json.loads(seen.read_text())
    assert "--pool-wait-timeout=11" in argv
    assert "--provider-stall-seconds=22" in argv
    assert "--stream-no-progress-seconds=33" in argv


def test_dispatcher_environment_is_trimmed_but_keeps_pool_tuning(tmp_path, monkeypatch):
    monkeypatch.setenv("CW_FAKE_TOKEN", "secret-value")
    monkeypatch.setenv("NEXUS_AGY_POOL_DIR", "/pool")
    dump = tmp_path / "env.json"
    worker, workdir, _ = _dispatched(
        tmp_path, f"open({str(dump)!r}, 'w').write(json.dumps(dict(os.environ)))"
    )
    worker.run(workdir, TASK)
    env = json.loads(dump.read_text())
    assert "CW_FAKE_TOKEN" not in env
    assert env["NEXUS_AGY_POOL_DIR"] == "/pool"
    assert env[ATTEMPT_ENV] == attempt(1) and env["HOME"] == os.environ["HOME"]


def test_whole_pool_exhaustion_is_its_own_failure(tmp_path):
    from cairnwork.workers.agy_dispatch import AgyPoolExhausted

    body = (
        "sys.stdin.read()\n"
        'sys.stderr.write(\'NEXUS_AGY_DISPATCH {"status": "pool_exhausted"}\\n\')\n'
        "sys.exit(75)"
    )
    worker, workdir, _ = _dispatched(tmp_path, body)
    with pytest.raises(AgyPoolExhausted, match="POOL_EXHAUSTED.*no other model"):
        worker.run(workdir, TASK)


def test_the_status_line_survives_a_flood_of_dispatcher_output(tmp_path):
    from cairnwork.workers.agy_dispatch import AgyPoolExhausted

    body = (
        f"sys.stdout.write('x' * {MAX_LOG_BYTES * 2})\n"
        'sys.stderr.write(\'NEXUS_AGY_DISPATCH {"status": "pool_exhausted"}\\n\')\n'
        "sys.exit(75)"
    )
    worker, workdir, _ = _dispatched(tmp_path, body)
    with pytest.raises(AgyPoolExhausted):
        worker.run(workdir, TASK)


@pytest.mark.parametrize(
    "body",
    [
        "sys.exit(75)",  # busy or failed, no status line
        'sys.stderr.write(\'NEXUS_AGY_DISPATCH {"status": "failed"}\\n\'); sys.exit(75)',
        'sys.stderr.write(\'NEXUS_AGY_DISPATCH {"status": "pool_exhausted"}\\n\'); sys.exit(1)',
        "sys.stderr.write('NEXUS_AGY_DISPATCH [not json\\n'); sys.exit(75)",
        "sys.exit(3)",
    ],
)
def test_other_dispatcher_failures_are_ordinary_worker_errors(tmp_path, body):
    from cairnwork.workers.agy_dispatch import AgyPoolExhausted

    worker, workdir, _ = _dispatched(tmp_path, body)
    with pytest.raises(AgyWorkerError) as raised:
        worker.run(workdir, TASK)
    assert not isinstance(raised.value, AgyPoolExhausted)


def test_a_missing_dispatcher_fails_the_run_not_falls_back_to_agy(tmp_path):
    from cairnwork.workers.agy_dispatch import Dispatch

    workdir = tmp_path / attempt(1)
    workdir.mkdir()
    worker = AgyWorker(dispatch=Dispatch(str(tmp_path / "gone")))
    with pytest.raises(AgyWorkerError, match="not found or not executable"):
        worker.run(workdir, TASK)


def test_without_a_dispatcher_the_agy_argv_is_unchanged(tmp_path):
    seen = tmp_path / "argv.json"
    stdin_seen = tmp_path / "stdin.txt"
    agy = fake_agy(
        tmp_path,
        f"open({str(seen)!r}, 'w').write(json.dumps(sys.argv))\n"
        f"open({str(stdin_seen)!r}, 'w').write(repr(sys.stdin.read()))",
    )
    workdir = tmp_path / attempt(1)
    workdir.mkdir()
    AgyWorker(agy=agy).run(workdir, TASK)
    argv = json.loads(seen.read_text())
    assert argv[0] == agy
    assert argv[1] == "-p" and argv[2].startswith("You are fixing a small bug")
    assert argv[3:] == [
        "--model", "gemini-3.8-flash-high",
        "--mode", "accept-edits",
        "--sandbox",
        "--print-timeout", "900s",
    ]  # fmt: skip
    assert stdin_seen.read_text() == "''"  # still no stdin


def test_a_timed_out_dispatcher_gets_sigterm_first_to_release_its_lease(tmp_path, monkeypatch):
    marker, ready = tmp_path / "term.marker", tmp_path / "ready"
    body = (
        "import signal, time\n"
        f"def term(*_):\n    open({str(marker)!r}, 'w').write('released'); sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, term)\n"
        f"open({str(ready)!r}, 'w').write('x')\n"
        "time.sleep(60)"
    )
    worker, workdir, _ = _dispatched(tmp_path, body)
    expire_once_ready(monkeypatch, ready)
    worker = AgyWorker(timeout_seconds=900, dispatch=worker.dispatch)
    with pytest.raises(AgyWorkerError, match="timed out"):
        worker.run(workdir, TASK)
    assert marker.read_text() == "released"


def test_a_timed_out_raw_agy_is_killed_without_sigterm(tmp_path, monkeypatch):
    marker, ready = tmp_path / "term.marker", tmp_path / "ready"
    agy = fake_agy(
        tmp_path,
        "import signal, time\n"
        f"signal.signal(signal.SIGTERM, lambda *_: open({str(marker)!r}, 'w').write('x'))\n"
        f"open({str(ready)!r}, 'w').write('x')\n"
        "time.sleep(60)",
    )
    workdir = tmp_path / attempt(1)
    workdir.mkdir()
    expire_once_ready(monkeypatch, ready)
    with pytest.raises(AgyWorkerError, match="timed out"):
        AgyWorker(agy=agy, timeout_seconds=900).run(workdir, TASK)
    assert not marker.exists()


EXHAUSTED = 'sys.stderr.write(\'NEXUS_AGY_DISPATCH {"status": "pool_exhausted"}\\n\')\n'


def test_the_last_status_line_wins(tmp_path):
    body = (
        EXHAUSTED + 'sys.stderr.write(\'NEXUS_AGY_DISPATCH {"status": "failed"}\\n\')\nsys.exit(75)'
    )
    worker, workdir, _ = _dispatched(tmp_path, body)
    with pytest.raises(AgyWorkerError) as raised:
        worker.run(workdir, TASK)
    assert type(raised.value) is AgyWorkerError


def test_a_non_executable_dispatcher_fails_with_the_usual_reason(tmp_path):
    worker, workdir, path = _dispatched(tmp_path, "pass")
    os.chmod(path, 0o644)
    with pytest.raises(AgyWorkerError, match=f"{path} not found or not executable"):
        worker.run(workdir, TASK)


def test_exhaustion_is_found_after_a_flood_of_trailing_diagnostics(tmp_path):
    from cairnwork.workers.agy_dispatch import AgyPoolExhausted

    body = EXHAUSTED + f"sys.stderr.write('d' * {MAX_LOG_BYTES * 2} + '\\n')\nsys.exit(75)"
    worker, workdir, _ = _dispatched(tmp_path, body)
    with pytest.raises(AgyPoolExhausted):
        worker.run(workdir, TASK)


def test_dispatcher_stderr_never_reaches_the_disk(tmp_path):
    body = f"sys.stderr.write(('e' * 1000 + '\\n') * {MAX_LOG_BYTES // 100})"
    worker, workdir, _ = _dispatched(tmp_path, body)
    worker.run(workdir, TASK)
    assert sum(f.stat().st_size for f in tmp_path.rglob("*") if f.is_file()) < MAX_LOG_BYTES


def test_a_file_named_like_an_option_stays_a_value(tmp_path):
    seen = tmp_path / "seen.json"
    task = SimpleNamespace(title="t", instructions="i", allowed_files=("-x.py",))
    worker, workdir, _ = _dispatched(
        tmp_path, f"open({str(seen)!r}, 'w').write(json.dumps(sys.argv))", task
    )
    worker.run(workdir, task)
    assert "--write-path=-x.py" in json.loads(seen.read_text())


def test_the_error_names_the_dispatchers_status_and_failure_kind(tmp_path):
    body = (
        "sys.stderr.write('NEXUS_AGY_DISPATCH "
        '{"status": "failed", "failure_kind": "AGY_WRITE_PATH_OUTSIDE_CWD"}\\n\')\n'
        "sys.exit(2)"
    )
    worker, workdir, _ = _dispatched(tmp_path, body)
    with pytest.raises(AgyWorkerError, match=r"exited 2.*failed.*AGY_WRITE_PATH_OUTSIDE_CWD"):
        worker.run(workdir, TASK)


def test_an_error_without_a_status_line_says_so(tmp_path):
    worker, workdir, _ = _dispatched(tmp_path, "sys.exit(3)")
    with pytest.raises(AgyWorkerError, match="exited 3; no dispatcher status"):
        worker.run(workdir, TASK)
