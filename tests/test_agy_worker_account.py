"""The Agy worker with state config agy_run_as_user (Issue #183): the dispatcher's
--run-as-user flag, fail-closed account failures, and the worker receipts that alone
prove the run ran as the worker account. Fake dispatcher replaying recorded stderr;
no model and no real account are used. The killed-dispatcher path is in
test_agy_worker_kill.py."""

import json

import pytest
from agy_receipt_lines import RUN_TAG, WORKER, cleanup, good_run, launch, status, sweep
from test_agy_worker import TASK, _dispatched, attempt, fake_dispatcher

from cairnwork.workers import agy_dispatch
from cairnwork.workers.agy import AgyWorker, AgyWorkerError
from cairnwork.workers.agy_dispatch import AgyWorkerAccountFailed, Dispatch

ROOT = f"{agy_dispatch.WORKER_RUNS}/{RUN_TAG}"


def _as_worker(tmp_path, body):
    path = fake_dispatcher(tmp_path, body)
    workdir = tmp_path / attempt(1)
    workdir.mkdir()
    dispatch = Dispatch(path, run_as_user=WORKER)
    return AgyWorker(model="m-1", timeout_seconds=900, dispatch=dispatch), workdir, path


def _replaying(tmp_path, stderr: str, code: int = 0, extra: str = ""):
    """A dispatcher that reads the prompt, does `extra`, replays `stderr` and exits."""
    recorded = tmp_path / "recorded-stderr.txt"
    recorded.write_text(stderr)
    body = (
        f"sys.stdin.read()\n{extra}\n"
        f"sys.stderr.write(open({str(recorded)!r}).read())\nsys.exit({code})"
    )
    return _as_worker(tmp_path, body)


def _record(workdir) -> dict:
    return json.loads((workdir.parent / f"{workdir.name}.agy.worker.json").read_text())


def test_run_as_user_reaches_the_dispatcher_as_its_own_flag(tmp_path):
    seen = tmp_path / "seen.json"
    extra = f"open({str(seen)!r}, 'w').write(json.dumps(sys.argv))"
    worker, workdir, path = _replaying(tmp_path, good_run(ROOT), extra=extra)
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
    worker.run(workdir, TASK)  # no status line, no receipts: unchanged behaviour
    assert not [a for a in json.loads(seen.read_text()) if "run-as-user" in a]
    assert not (tmp_path / f"{workdir.name}.agy.worker.json").exists()


def test_a_run_with_clean_receipts_succeeds_and_records_them(tmp_path):
    worker, workdir, _ = _replaying(tmp_path, good_run(ROOT))
    worker.run(workdir, TASK)
    record = _record(workdir)
    assert record["isolated"] is True and record["problems"] == []
    (only,) = record["attempts"]
    assert (only["run_as_user"], only["run_root"]) == (WORKER, ROOT)
    assert only["run_root_removed"] is True and only["worker_failure"] is None
    assert only["sweep"] == {"result": "clean", "wrapper": "CLEAN", "left": []}
    assert record["final_status"]["status"] == "completed" and record["post_hoc_sweeps"] == {}


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        (status(), "WORKER_RECEIPT_MISSING"),  # e.g. a dispatcher that ignored the flag
        ("", "DISPATCH_STATUS_MISSING"),  # nothing at all on stderr
        (good_run(ROOT, user="james"), "WORKER_RECEIPT_MISMATCH"),  # the wrong account
        (good_run("/Users/Shared/elsewhere/" + RUN_TAG), "WORKER_RECEIPT_MISMATCH"),
        ("".join("[provider] " + x for x in (launch(ROOT), sweep(), cleanup())) + status(),
         "WORKER_RECEIPT_MISSING"),  # forged by the worker, neutralised by the dispatcher
        (launch(ROOT) + sweep() + cleanup(), "DISPATCH_STATUS_MISSING"),  # no final status
        (good_run(ROOT) + 'NEXUS_AGY_DISPATCH {"late": 1}\n', "DISPATCH_STATUS_MISSING"),
    ],
    ids=["no-launch", "empty", "wrong-user", "wrong-root", "forged", "no-final", "not-last"],
)  # fmt: skip
def test_an_exit_0_run_without_proof_of_the_worker_account_fails(tmp_path, stderr, code):
    worker, workdir, _ = _replaying(tmp_path, stderr)
    with pytest.raises(AgyWorkerAccountFailed, match=f"^{code}:"):
        worker.run(workdir, TASK)
    assert _record(workdir)["isolated"] is False
    assert _record(workdir)["problems"][-1].startswith(code)


@pytest.mark.parametrize(
    ("bad", "code"),
    [
        (sweep(result="unclean", wrapper="UNCLEAN", left=["4242"]) + cleanup(),
         "WORKER_SWEEP_UNCLEAN"),
        (sweep(result="unclean", wrapper="REFUSED") + cleanup(), "WORKER_SWEEP_UNCLEAN"),
        (sweep(result="error", wrapper="SWEEP_ERROR") + cleanup(), "WORKER_SWEEP_UNCLEAN"),
        (sweep() + cleanup(removed=False), "WORKER_RUN_ROOT_LEFT"),
        (sweep() + cleanup(failure="AGY_WORKER_COPY_BACK_FAILED:partial"),
         "WORKER_ACCOUNT_FAILED"),
        ("", "WORKER_RECEIPT_INCOMPLETE"),
    ],
)  # fmt: skip
@pytest.mark.parametrize("exit_code", [0, 1])
def test_an_unclean_graceful_end_fails_closed_whatever_the_exit_code(
    tmp_path, monkeypatch, bad, code, exit_code
):
    sudo = tmp_path / "sudo-called"
    monkeypatch.setattr(agy_dispatch, "sweep_worker_run", lambda *a: sudo.write_text("x"))
    stderr = launch(ROOT) + bad + status("completed" if exit_code == 0 else "failed")
    worker, workdir, _ = _replaying(tmp_path, stderr, exit_code)
    # a run root gone without a clean sweep receipt may have left a worker: that leads
    swept = all(f'"{k}": {v}' in bad for k, v in (("result", '"clean"'), ("left", "[]")))
    primary = code if swept and '"wrapper": "CLEAN"' in bad else "WORKER_SWEEP_UNCLEAN"
    with pytest.raises((AgyWorkerError, AgyWorkerAccountFailed), match=f"^{primary}:") as raised:
        worker.run(workdir, TASK)
    assert "attempt 1" in str(raised.value) and code in str(raised.value)
    assert not sudo.exists()  # the root is gone: nothing to sweep (the wrapper would say rc 64)
    assert _record(workdir)["attempts"][0]["run_root"] == ROOT


REFUSED = (
    "DISPATCH_CONTRACT_REJECTED:AGY_WRITE_BOUNDARY_PREPARE_FAILED:"
    "AGY_WRITE_BOUNDARY_UNAVAILABLE:AGY_WORKER_ACCOUNT_REFUSED:wrapper_missing"
)


@pytest.mark.parametrize(
    ("state", "kind", "code"),
    [
        ("invalid_contract", REFUSED, 64),
        ("failed", "AGY_WORKER_LAUNCH_FAILED:wrapper_did_not_start", 1),
        ("invalid_contract", "RUN_AS_USER_REQUIRES_ACCEPT_EDITS", 64),
        ("failed", "AGY_WORKER_COPY_BACK_REFUSED:symlink", 1),
        ("failed", "AGY_WORKER_LAUNCH_FAILED:wrapper_did_not_start", 0),  # fail closed
    ],
)
def test_a_refused_worker_account_fails_the_attempt_with_its_own_code(tmp_path, state, kind, code):
    worker, workdir, _ = _replaying(tmp_path, status(state, failure_kind=kind), code)
    with pytest.raises(AgyWorkerAccountFailed, match="WORKER_ACCOUNT_FAILED: .*" + kind) as raised:
        worker.run(workdir, TASK)
    assert "not run as the owner" in str(raised.value)


def test_a_launch_that_never_started_still_names_the_account_failure(tmp_path):
    failure = "AGY_WORKER_LAUNCH_FAILED:wrapper_did_not_start"
    stderr = (
        launch(ROOT)
        + sweep(result="not_started", wrapper="REFUSED")
        + cleanup(failure=failure)
        + status("failed", failure_kind=failure)
    )
    worker, workdir, _ = _replaying(tmp_path, stderr, 1)
    # not_started is no clean sweep receipt: the unswept run leads, the account failure follows
    with pytest.raises(AgyWorkerError, match=f"^WORKER_SWEEP_UNCLEAN: .*{failure}"):
        worker.run(workdir, TASK)
    assert _record(workdir)["attempts"][0]["worker_failure"] == failure


def test_an_ordinary_dispatcher_failure_under_run_as_user_is_not_an_account_failure(tmp_path):
    stderr = good_run(ROOT).replace(
        status(), status("failed", failure_kind="AGY_WRITE_PATH_OUTSIDE_CWD")
    )
    worker, workdir, _ = _replaying(tmp_path, stderr, 2)
    with pytest.raises(AgyWorkerError, match="exited 2"):
        worker.run(workdir, TASK)


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
