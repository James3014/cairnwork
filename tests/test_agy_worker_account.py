"""The Agy worker with state config agy_run_as_user (Issue #183): the dispatcher's
--run-as-user flag, fail-closed account failures, and the wrapper sweep. Fake dispatcher,
fake sudo; no model and no real account are used."""

import json
import os
import shutil
import subprocess
import sys

import pytest
from test_agy_worker import TASK, _dispatched, attempt, expire_once_ready, fake_dispatcher

from cairnwork.workers.agy import AgyWorker, AgyWorkerError

WORKER = "_cairnwork_worker"
RUN_TAG = "0123456789abcdef" * 2


def _as_worker(tmp_path, body, task=TASK):
    from cairnwork.workers.agy_dispatch import Dispatch

    path = fake_dispatcher(tmp_path, body)
    workdir = tmp_path / attempt(1)
    workdir.mkdir()
    dispatch = Dispatch(path, run_as_user=WORKER)
    return AgyWorker(model="m-1", timeout_seconds=900, dispatch=dispatch), workdir, path


def test_run_as_user_reaches_the_dispatcher_as_its_own_flag(tmp_path):
    seen = tmp_path / "seen.json"
    worker, workdir, path = _as_worker(
        tmp_path, f"open({str(seen)!r}, 'w').write(json.dumps(sys.argv))"
    )
    worker.run(workdir, TASK)
    argv = json.loads(seen.read_text())
    assert argv[:2] == [path, f"--cwd={workdir}"]
    assert argv[-2:] == [f"--run-as-user={WORKER}", "--write-path=a.py"]


def test_without_run_as_user_the_dispatcher_argv_carries_no_account(tmp_path):
    seen = tmp_path / "seen.json"
    worker, workdir, _ = _dispatched(
        tmp_path, f"open({str(seen)!r}, 'w').write(json.dumps(sys.argv))"
    )
    assert worker.dispatch is not None and worker.dispatch.run_as_user is None
    worker.run(workdir, TASK)
    assert not [a for a in json.loads(seen.read_text()) if "run-as-user" in a]


REFUSED = (
    "DISPATCH_CONTRACT_REJECTED:AGY_WRITE_BOUNDARY_PREPARE_FAILED:"
    "AGY_WRITE_BOUNDARY_UNAVAILABLE:AGY_WORKER_ACCOUNT_REFUSED:wrapper_missing"
)


@pytest.mark.parametrize(
    ("status", "kind", "code"),
    [
        ("invalid_contract", REFUSED, 64),
        ("failed", "AGY_WORKER_LAUNCH_FAILED:wrapper_did_not_start", 1),
        ("invalid_contract", "RUN_AS_USER_REQUIRES_ACCEPT_EDITS", 64),
        ("failed", "AGY_WORKER_COPY_BACK_REFUSED:symlink", 1),
        ("failed", "AGY_WORKER_LAUNCH_FAILED:wrapper_did_not_start", 0),  # fail closed
    ],
)
def test_a_refused_worker_account_fails_the_attempt_with_its_own_code(tmp_path, status, kind, code):
    from cairnwork.workers.agy_dispatch import AgyWorkerAccountFailed

    line = json.dumps({"status": status, "failure_kind": kind})
    body = (
        f"sys.stdin.read()\nsys.stderr.write('NEXUS_AGY_DISPATCH ' + {line!r} + '\\n')\n"
        f"sys.exit({code})"
    )
    worker, workdir, _ = _as_worker(tmp_path, body)
    with pytest.raises(AgyWorkerAccountFailed, match="WORKER_ACCOUNT_FAILED: .*" + kind) as raised:
        worker.run(workdir, TASK)
    assert "not run as the owner" in str(raised.value)


def test_an_ordinary_dispatcher_failure_under_run_as_user_is_not_an_account_failure(tmp_path):
    from cairnwork.workers.agy_dispatch import AgyWorkerAccountFailed

    body = (
        "sys.stderr.write('NEXUS_AGY_DISPATCH "
        '{"status": "failed", "failure_kind": "AGY_WRITE_PATH_OUTSIDE_CWD"}\\n\')\n'
        "sys.exit(2)"
    )
    worker, workdir, _ = _as_worker(tmp_path, body)
    with pytest.raises(AgyWorkerError, match="exited 2") as raised:
        worker.run(workdir, TASK)
    assert not isinstance(raised.value, AgyWorkerAccountFailed)


def test_a_dispatcher_without_the_flag_rejects_it_and_nothing_runs_as_the_owner(tmp_path):
    ran = tmp_path / "ran"
    body = (
        "if any(a.startswith('--run-as-user') for a in sys.argv):\n"
        "    sys.stderr.write('usage: dispatch: unrecognized arguments\\n'); sys.exit(2)\n"
        f"open({str(ran)!r}, 'w').write('x')"
    )
    worker, workdir, _ = _as_worker(tmp_path, body)
    with pytest.raises(AgyWorkerError, match="exited 2; no dispatcher status"):
        worker.run(workdir, TASK)
    assert not ran.exists()


def _worker_layout(tmp_path, monkeypatch, sweep_body):
    """A fake sudo, wrapper path and worker-runs directory holding one run of RUN_TAG."""
    from cairnwork.workers import agy_dispatch

    sudo = tmp_path / "sudo"
    sudo.write_text(f"#!{sys.executable}\nimport json, sys\n{sweep_body}\n")
    sudo.chmod(0o755)
    runs = tmp_path / "worker-runs"
    run_root = runs / RUN_TAG
    (run_root / "home").mkdir(parents=True)
    (run_root / "home" / "session.json").write_text("{}")
    monkeypatch.setattr(agy_dispatch, "SUDO", str(sudo))
    monkeypatch.setattr(agy_dispatch, "WORKER_WRAPPER", str(tmp_path / "agy-worker-exec"))
    monkeypatch.setattr(agy_dispatch, "WORKER_RUNS", str(runs))
    monkeypatch.setattr(agy_dispatch, "TERM_GRACE_SECONDS", 1)
    return run_root


def _launching_dispatcher(tmp_path, monkeypatch, run_root, ready):
    """A dispatcher that has launched its worker the way the process list shows it (`-u
    <account> <wrapper> <run_root> ...`, the dispatcher as parent), ignores SIGTERM, and
    only then lets the worker's timeout expire."""
    from cairnwork.workers import agy_dispatch

    body = (
        "import signal, subprocess, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', '-n', '-u',"
        f" {WORKER!r}, {agy_dispatch.WORKER_WRAPPER!r}, {str(run_root)!r}, 'work'])\n"
        f"time.sleep(0.5); open({str(ready)!r}, 'w').write('x')\n"
        "time.sleep(60)"
    )
    worker, workdir, path = _as_worker(tmp_path, body)
    expire_once_ready(monkeypatch, ready, only=path)
    return worker, workdir


def test_a_killed_worker_account_run_is_swept_through_the_wrapper(tmp_path, monkeypatch):
    seen = tmp_path / "sweep-argv.json"
    run_root = _worker_layout(
        tmp_path,
        monkeypatch,
        f"open({str(seen)!r}, 'w').write(json.dumps(sys.argv))\nprint('AGY_WORKER_SWEEP clean')",
    )
    worker, workdir = _launching_dispatcher(tmp_path, monkeypatch, run_root, tmp_path / "ready")
    with pytest.raises(AgyWorkerError, match="agy timed out"):
        worker.run(workdir, TASK)
    from cairnwork.workers import agy_dispatch

    assert json.loads(seen.read_text())[1:] == [
        "-n", "-u", WORKER, agy_dispatch.WORKER_WRAPPER, "--sweep", str(run_root),
    ]  # fmt: skip
    assert not run_root.exists()  # the per-run HOME (session files) does not linger


@pytest.mark.parametrize(
    "sweep_body",
    [
        "print('AGY_WORKER_SWEEP unclean 4242'); sys.exit(3)",
        "print('AGY_WORKER_SWEEP clean'); sys.exit(1)",  # only a clean exit 0 counts
        "sys.stderr.write('agy-worker-exec: refusing: run_root is a symlink\\n'); sys.exit(64)",
        "print('AGY_WORKER_SWEEP clean\\nsomething after')",
    ],
)
def test_an_unclean_or_refused_sweep_fails_the_attempt_and_keeps_the_run(
    tmp_path, monkeypatch, sweep_body
):
    run_root = _worker_layout(tmp_path, monkeypatch, sweep_body)
    worker, workdir = _launching_dispatcher(tmp_path, monkeypatch, run_root, tmp_path / "ready")
    with pytest.raises(AgyWorkerError, match=f"WORKER_SWEEP_UNCLEAN.*{RUN_TAG}"):
        worker.run(workdir, TASK)
    assert run_root.exists()


def test_a_run_the_dispatcher_already_removed_is_not_swept(tmp_path, monkeypatch):
    seen = tmp_path / "sweep-argv.json"
    run_root = _worker_layout(tmp_path, monkeypatch, f"open({str(seen)!r}, 'w').write('called')")
    shutil.rmtree(run_root)  # the dispatcher removes it only after its own clean sweep
    worker, workdir = _launching_dispatcher(tmp_path, monkeypatch, run_root, tmp_path / "ready")
    with pytest.raises(AgyWorkerError, match="agy timed out"):
        worker.run(workdir, TASK)
    assert not seen.exists()


def test_an_unreadable_process_list_counts_as_an_unswept_run(tmp_path, monkeypatch):
    from cairnwork.workers import agy_dispatch

    run_root = _worker_layout(tmp_path, monkeypatch, "print('AGY_WORKER_SWEEP clean')")
    worker, workdir = _launching_dispatcher(tmp_path, monkeypatch, run_root, tmp_path / "ready")
    monkeypatch.setattr(agy_dispatch, "worker_run_roots", lambda _pid: None)
    with pytest.raises(AgyWorkerError, match="WORKER_SWEEP_UNCLEAN.*unknown"):
        worker.run(workdir, TASK)
    assert run_root.exists()


def test_worker_run_roots_lists_only_the_dispatchers_own_well_formed_launches(
    tmp_path, monkeypatch
):
    from cairnwork.workers import agy_dispatch

    wrapper, runs = str(tmp_path / "agy-worker-exec"), str(tmp_path / "worker-runs")
    monkeypatch.setattr(agy_dispatch, "WORKER_WRAPPER", wrapper)
    monkeypatch.setattr(agy_dispatch, "WORKER_RUNS", runs)
    sleep = [sys.executable, "-c", "import time; time.sleep(30)"]
    children = [
        subprocess.Popen(sleep + ["-n", "-u", WORKER, wrapper, f"{runs}/{RUN_TAG}", "work"]),
        subprocess.Popen(sleep + ["-n", "-u", WORKER, wrapper, f"{runs}/{'f' * 32}"]),
        subprocess.Popen(sleep + ["-n", "-u", WORKER, wrapper, f"{runs}/not-a-run-tag"]),
        subprocess.Popen(sleep + ["-n", "-u", WORKER, wrapper, f"{runs}/{'0' * 32}/work"]),
        subprocess.Popen(sleep + ["-n", WORKER, wrapper, f"{runs}/{'1' * 32}"]),  # no -u
        subprocess.Popen(sleep + [wrapper, "--sweep", f"{runs}/{'2' * 32}"]),
    ]
    try:
        found = agy_dispatch.worker_run_roots(os.getpid())
        assert found == {f"{runs}/{RUN_TAG}", f"{runs}/{'f' * 32}"}
        assert agy_dispatch.worker_run_roots(children[0].pid) == set()  # another parent
    finally:
        for child in children:
            child.kill()
            child.wait()
