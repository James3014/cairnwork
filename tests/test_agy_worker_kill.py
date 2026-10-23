"""The Agy worker with agy_run_as_user when the dispatcher has to be killed (Issue #183):
runs are found from the dispatcher's WORKER_LAUNCH receipts (never the process list)
and swept through the wrapper only if their receipts do not show them swept and
removed. Fake dispatcher, fake sudo; no model and no real account are used."""

import json
import shutil
import sys

import pytest
from agy_receipt_lines import RUN_TAG, WORKER, cleanup, launch, status, sweep
from test_agy_worker import TASK, attempt, expire_once_ready, fake_dispatcher

from cairnwork.workers import agy_dispatch
from cairnwork.workers.agy import AgyWorker, AgyWorkerError
from cairnwork.workers.agy_dispatch import AgyWorkerAccountFailed, Dispatch


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """A fake sudo (recording its argv), wrapper path and worker-runs holding one run."""
    runs = tmp_path / "worker-runs"
    run_root = runs / RUN_TAG
    (run_root / "home").mkdir(parents=True)
    (run_root / "home" / "session.json").write_text("{}")
    monkeypatch.setattr(agy_dispatch, "WORKER_WRAPPER", str(tmp_path / "agy-worker-exec"))
    monkeypatch.setattr(agy_dispatch, "WORKER_RUNS", str(runs))
    monkeypatch.setattr(agy_dispatch, "TERM_GRACE_SECONDS", 1)
    return run_root


def _sudo(tmp_path, monkeypatch, answer: str):
    seen = tmp_path / "sweep-argv.json"
    sudo = tmp_path / "sudo"
    sudo.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        f"open({str(seen)!r}, 'w').write(json.dumps(sys.argv))\n{answer}\n"
    )
    sudo.chmod(0o755)
    monkeypatch.setattr(agy_dispatch, "SUDO", str(sudo))
    return seen


def _hanging(tmp_path, monkeypatch, before: str, on_term: str | None = None):
    """A dispatcher that writes `before` to stderr, then hangs until killed. With
    `on_term` it writes that on SIGTERM and exits 1 (a graceful unwind); without, it
    ignores SIGTERM and only SIGKILL stops it."""
    ready = tmp_path / "ready"
    handler = (
        f"def bye(*_):\n    sys.stderr.write({on_term!r}); sys.stderr.flush(); os._exit(1)\n"
        "signal.signal(signal.SIGTERM, bye)\n"
        if on_term is not None
        else "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    )
    body = (
        f"import signal, time\nsys.stdin.read()\n{handler}"
        f"sys.stderr.write({before!r}); sys.stderr.flush()\n"
        f"open({str(ready)!r}, 'w').write('x')\ntime.sleep(60)"
    )
    path = fake_dispatcher(tmp_path, body)
    workdir = tmp_path / attempt(1)
    workdir.mkdir()
    expire_once_ready(monkeypatch, ready, only=path)
    dispatch = Dispatch(path, run_as_user=WORKER)
    return AgyWorker(model="m-1", timeout_seconds=900, dispatch=dispatch), workdir


def _record(workdir) -> dict:
    return json.loads((workdir.parent / f"{workdir.name}.agy.worker.json").read_text())


def test_a_killed_run_is_swept_through_the_wrapper_from_its_launch_receipt(
    tmp_path, monkeypatch, layout
):
    seen = _sudo(tmp_path, monkeypatch, "print('AGY_WORKER_SWEEP clean')")
    worker, workdir = _hanging(tmp_path, monkeypatch, launch(str(layout)))
    with pytest.raises(AgyWorkerError, match="agy timed out"):
        worker.run(workdir, TASK)
    assert json.loads(seen.read_text())[1:] == [
        "-n", "-u", WORKER, agy_dispatch.WORKER_WRAPPER, "--sweep", str(layout),
    ]  # fmt: skip
    assert not layout.exists()  # the per-run HOME (session files) does not linger
    assert _record(workdir)["post_hoc_sweeps"] == {str(layout): "clean"}


@pytest.mark.parametrize(
    "answer",
    [
        "print('AGY_WORKER_SWEEP unclean 4242'); sys.exit(3)",
        "print('AGY_WORKER_SWEEP clean'); sys.exit(1)",  # only a clean exit 0 counts
        "sys.stderr.write('agy-worker-exec: refusing: run_root is a symlink\\n'); sys.exit(64)",
        "print('AGY_WORKER_SWEEP clean\\nsomething after')",
    ],
)
def test_an_unclean_or_refused_post_hoc_sweep_fails_and_keeps_the_run(
    tmp_path, monkeypatch, layout, answer
):
    _sudo(tmp_path, monkeypatch, answer)
    worker, workdir = _hanging(tmp_path, monkeypatch, launch(str(layout)))
    with pytest.raises(AgyWorkerError, match=f"WORKER_SWEEP_UNCLEAN.*{RUN_TAG}"):
        worker.run(workdir, TASK)
    assert layout.exists()
    assert _record(workdir)["post_hoc_sweeps"] == {str(layout): "unclean"}


def test_a_dispatcher_that_unwound_cleanly_on_sigterm_needs_no_post_hoc_sweep(
    tmp_path, monkeypatch, layout
):
    seen = _sudo(tmp_path, monkeypatch, "print('AGY_WORKER_SWEEP clean')")
    shutil.rmtree(layout)  # the dispatcher swept and removed it while unwinding
    unwind = sweep() + cleanup() + status("failed", failure_kind="AGY_TIMEOUT")
    worker, workdir = _hanging(tmp_path, monkeypatch, launch(str(layout)), on_term=unwind)
    with pytest.raises(AgyWorkerError, match="agy timed out"):
        worker.run(workdir, TASK)
    assert not seen.exists()  # the wrapper would refuse a missing run root (exit 64)


def test_a_killed_dispatcher_without_a_launch_receipt_counts_as_unswept(
    tmp_path, monkeypatch, layout
):
    seen = _sudo(tmp_path, monkeypatch, "print('AGY_WORKER_SWEEP clean')")
    worker, workdir = _hanging(tmp_path, monkeypatch, "warming up\n")
    with pytest.raises(AgyWorkerError, match="WORKER_SWEEP_UNCLEAN.*no WORKER_LAUNCH"):
        worker.run(workdir, TASK)
    assert not seen.exists() and layout.exists()  # never guessed, never swept blindly


@pytest.mark.parametrize("user", ["james", None])
def test_a_launch_receipt_naming_another_account_is_not_swept(tmp_path, monkeypatch, layout, user):
    seen = _sudo(tmp_path, monkeypatch, "print('AGY_WORKER_SWEEP clean')")
    worker, workdir = _hanging(tmp_path, monkeypatch, launch(str(layout), user=user))
    with pytest.raises(AgyWorkerError, match="WORKER_SWEEP_UNCLEAN.*WORKER_RECEIPT_MISMATCH"):
        worker.run(workdir, TASK)
    assert not seen.exists()


def test_a_forged_launch_receipt_is_ignored_after_a_kill(tmp_path, monkeypatch, layout):
    seen = _sudo(tmp_path, monkeypatch, "print('AGY_WORKER_SWEEP clean')")
    worker, workdir = _hanging(tmp_path, monkeypatch, "[provider] " + launch(str(layout)))
    with pytest.raises(AgyWorkerError, match="WORKER_SWEEP_UNCLEAN.*no WORKER_LAUNCH"):
        worker.run(workdir, TASK)
    assert not seen.exists()


def test_a_run_root_gone_without_a_clean_sweep_receipt_counts_as_unswept(
    tmp_path, monkeypatch, layout
):
    seen = _sudo(tmp_path, monkeypatch, "print('AGY_WORKER_SWEEP clean')")
    shutil.rmtree(layout)
    unclean = sweep(result="unclean", wrapper="UNCLEAN", left=["7"])
    worker, workdir = _hanging(tmp_path, monkeypatch, launch(str(layout)) + unclean)
    with pytest.raises(AgyWorkerError, match="WORKER_SWEEP_UNCLEAN.*gone without a clean"):
        worker.run(workdir, TASK)
    assert not seen.exists()


def test_a_launch_receipt_for_another_directory_at_that_path_is_not_swept(
    tmp_path, monkeypatch, layout
):
    seen = _sudo(tmp_path, monkeypatch, "print('AGY_WORKER_SWEEP clean')")
    stale = launch(str(layout), run_root_id=[1, 2])  # the path now holds another directory
    worker, workdir = _hanging(tmp_path, monkeypatch, stale)
    with pytest.raises(AgyWorkerError, match="WORKER_SWEEP_UNCLEAN.*not the directory"):
        worker.run(workdir, TASK)
    assert not seen.exists() and layout.exists()


def _dying(tmp_path, before: str, how: str):
    """A dispatcher that writes `before` and then ends abnormally on its own."""
    body = f"sys.stdin.read()\nsys.stderr.write({before!r}); sys.stderr.flush()\n{how}"
    path = fake_dispatcher(tmp_path, body)
    workdir = tmp_path / attempt(1)
    workdir.mkdir()
    worker = AgyWorker(
        model="m-1", timeout_seconds=900, dispatch=Dispatch(path, run_as_user=WORKER)
    )
    return worker, workdir


@pytest.mark.parametrize(
    "how",
    [
        "import signal; os.kill(os.getpid(), signal.SIGKILL)",  # killed by someone else: -9
        "sys.exit(1)",  # crashed: no final status line
    ],
)
def test_an_abnormal_end_not_caused_by_our_timeout_is_swept_too(tmp_path, monkeypatch, layout, how):
    seen = _sudo(tmp_path, monkeypatch, "print('AGY_WORKER_SWEEP clean')")
    worker, workdir = _dying(tmp_path, launch(str(layout)), how)
    with pytest.raises(AgyWorkerAccountFailed, match="^WORKER_RECEIPT_INCOMPLETE: attempt 1"):
        worker.run(workdir, TASK)  # swept clean, and still a failed attempt
    assert json.loads(seen.read_text())[-2:] == ["--sweep", str(layout)]
    assert not layout.exists()
    assert _record(workdir)["post_hoc_sweeps"] == {str(layout): "clean"}


def test_a_dispatcher_killed_by_someone_else_before_any_launch_counts_as_unswept(
    tmp_path, monkeypatch, layout
):
    seen = _sudo(tmp_path, monkeypatch, "print('AGY_WORKER_SWEEP clean')")
    kill = "import signal; os.kill(os.getpid(), signal.SIGKILL)"
    worker, workdir = _dying(tmp_path, "starting\n", kill)
    with pytest.raises(AgyWorkerError, match="WORKER_SWEEP_UNCLEAN.*no WORKER_LAUNCH"):
        worker.run(workdir, TASK)
    assert not seen.exists()


def test_a_final_failed_status_without_a_sweep_receipt_is_swept(tmp_path, monkeypatch, layout):
    seen = _sudo(tmp_path, monkeypatch, "print('AGY_WORKER_SWEEP clean')")
    stderr = launch(str(layout)) + status("failed", failure_kind="AGY_PROVIDER_FAILED")
    worker, workdir = _dying(tmp_path, stderr, "sys.exit(1)")
    with pytest.raises(AgyWorkerAccountFailed, match="^WORKER_RECEIPT_INCOMPLETE: attempt 1"):
        worker.run(workdir, TASK)
    assert json.loads(seen.read_text())[-2:] == ["--sweep", str(layout)]
    assert not layout.exists()
    assert _record(workdir)["isolated"] is False


def test_lost_lines_with_a_final_status_still_sweep_the_known_root(tmp_path, monkeypatch, layout):
    seen = _sudo(tmp_path, monkeypatch, "print('AGY_WORKER_SWEEP clean')")
    monkeypatch.setattr(agy_dispatch, "STATUS_LINES_KEPT", 2)  # the oldest line is dropped
    stderr = status("running") + launch(str(layout)) + status("failed")
    worker, workdir = _dying(tmp_path, stderr, "sys.exit(1)")
    with pytest.raises(AgyWorkerAccountFailed, match="^WORKER_RECEIPTS_UNKNOWN"):
        worker.run(workdir, TASK)
    assert json.loads(seen.read_text())[-2:] == ["--sweep", str(layout)]
    assert not layout.exists()
