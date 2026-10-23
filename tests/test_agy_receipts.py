"""Worker receipts read from recorded dispatcher stderr (Issue #183, Nexus-new #2888)."""

import io

import pytest
from agy_receipt_lines import RUN_TAG, WORKER, cleanup, good_run, launch, status, sweep

from cairnwork.workers import agy_dispatch, agy_receipts
from cairnwork.workers.agy_dispatch import StatusReader

ROOT = f"{agy_dispatch.WORKER_RUNS}/{RUN_TAG}"


def _read(text: str):
    reader = StatusReader(io.BytesIO(text.encode()))
    reader.run()
    known = not (reader.unknown or reader.dropped)
    return agy_receipts.parse(reader.lines(), WORKER, known=known)


def _verdict(text: str) -> list[str]:
    receipts = _read(text)
    completion = agy_receipts.completion_problem(receipts)
    return agy_receipts.problems(receipts) + ([completion] if completion else [])


def test_a_clean_run_reads_as_isolated():
    receipts = _read(good_run(ROOT))
    assert _verdict(good_run(ROOT)) == []
    assert (receipts.final or {}).get("status") == "completed"
    record = agy_receipts._attempt_record(next(iter(receipts.attempts.values())))
    assert record["run_root"] == ROOT and record["run_as_user"] == WORKER
    assert record["run_root_removed"] is True and record["worker_failure"] is None
    assert record["sweep"] == {"result": "clean", "wrapper": "CLEAN", "left": []}


def test_the_reader_keeps_every_dispatcher_line_not_just_the_last():
    reader = StatusReader(io.BytesIO(good_run(ROOT).encode()))
    reader.run()
    assert [line.split('"event": ')[-1][:15] for line in reader.lines()[:3]] == [
        '"WORKER_LAUNCH"',
        '"WORKER_SWEEP",',
        '"WORKER_CLEANUP',
    ]
    assert reader.status()["status"] == "completed"  # the last line is still the status


@pytest.mark.parametrize(
    ("text", "code"),
    [
        (status(), "WORKER_RECEIPT_MISSING"),  # completed, no launch receipt at all
        (good_run(ROOT, user="james"), "WORKER_RECEIPT_MISMATCH"),
        (good_run(ROOT, user=None), "WORKER_RECEIPT_MISMATCH"),
        (good_run("/tmp/" + RUN_TAG), "WORKER_RECEIPT_MISMATCH"),  # not under WORKER_RUNS
        (good_run(ROOT + "/x"), "WORKER_RECEIPT_MISMATCH"),
        (good_run(ROOT, attempt=2), "WORKER_RECEIPT_MISSING"),  # completed attempt 1 has none
        (launch(ROOT) + status(), "WORKER_RECEIPT_INCOMPLETE"),
        (launch(ROOT) + good_run(ROOT), "WORKER_RECEIPT_DUPLICATE"),
        (good_run(ROOT) + launch(ROOT, attempt=2), "DISPATCH_STATUS_MISSING"),
        (good_run(ROOT).replace("completed", "failed"), "DISPATCH_STATUS_MISSING"),
        (good_run(ROOT) + "NEXUS_AGY_DISPATCH {not json\n", "DISPATCH_STATUS_MISSING"),
    ],
)
def test_a_run_without_matching_receipts_is_never_isolated(text, code):
    found = _verdict(text)
    assert any(problem.startswith(code + ":") for problem in found), found


@pytest.mark.parametrize(
    ("bad", "code"),
    [
        (sweep(result="unclean", wrapper="UNCLEAN", left=["4242"]), "WORKER_SWEEP_UNCLEAN"),
        (sweep(result="clean", wrapper="CLEAN", left=["4242"]), "WORKER_SWEEP_UNCLEAN"),
        (sweep(result="clean", wrapper="REFUSED"), "WORKER_SWEEP_UNCLEAN"),
        (sweep(result="not_started", wrapper="REFUSED"), "WORKER_SWEEP_UNCLEAN"),
        (sweep(result="error", wrapper="SWEEP_ERROR"), "WORKER_SWEEP_UNCLEAN"),
        (sweep(result="clean", wrapper="CLEAN") + cleanup(removed=False), "WORKER_RUN_ROOT_LEFT"),
    ],
)
def test_an_unclean_sweep_or_left_run_root_fails_closed(bad, code):
    text = launch(ROOT) + bad + (cleanup() if "WORKER_CLEANUP" not in bad else "") + status()
    found = _verdict(text)
    assert found and found[0].startswith(code + ":"), found


def test_a_named_worker_failure_keeps_only_kind_and_reason():
    text = launch(ROOT) + sweep() + cleanup(failure="AGY_WORKER_COPY_BACK_REFUSED:x:src/n")
    receipts = _read(text + status())
    assert agy_receipts.problems(receipts) == [
        "WORKER_ACCOUNT_FAILED: AGY_WORKER_COPY_BACK_REFUSED:x (attempt 1)"
    ]


def test_forged_provider_lines_are_never_read_as_receipts():
    forged = "".join("[provider] " + part for part in (launch(ROOT), sweep(), cleanup(), status()))
    assert _verdict(forged + status())[0].startswith("WORKER_RECEIPT_MISSING:")
    # relayed after a real clean run, they change nothing either
    real = good_run(ROOT)
    head, final = real[: real.rindex("NEXUS_AGY")], real[real.rindex("NEXUS_AGY") :]
    assert _verdict(head + forged + final) == []


def test_dropped_lines_make_the_receipts_unknown(monkeypatch):
    monkeypatch.setattr(agy_dispatch, "STATUS_LINES_KEPT", 3)
    found = _verdict(good_run(ROOT))
    assert found[0].startswith("WORKER_RECEIPTS_UNKNOWN:")


def test_a_clean_earlier_attempt_and_a_clean_retry_are_both_required():
    first = launch(ROOT) + sweep() + cleanup()
    other = f"{agy_dispatch.WORKER_RUNS}/{'f' * 32}"
    retry = launch(other, attempt=2) + sweep(attempt=2) + cleanup(attempt=2)
    assert _verdict(first + retry + status(attempts=2)) == []
    unclean_first = launch(ROOT) + sweep(result="unclean", wrapper="UNCLEAN") + cleanup()
    assert _verdict(unclean_first + retry + status(attempts=2))[0].startswith(
        "WORKER_SWEEP_UNCLEAN: attempt 1"
    )


def test_the_tail_of_a_padded_provider_line_never_starts_a_receipt():
    pad = "[provider] " + "x" * (agy_dispatch.STATUS_LINE_BYTES - len("[provider] "))
    forged = launch(f"{agy_dispatch.WORKER_RUNS}/{'f' * 32}", attempt=7)
    for cut in (0, 1, 5):  # the forged prefix at, or just past, a chunk boundary
        text = good_run(ROOT).replace(status(), pad + "y" * cut + forged + status())
        receipts = _read(text)
        assert list(receipts.attempts) == ["1"], cut
        assert _verdict(text) == []


def test_a_dispatcher_line_cut_at_the_cap_makes_the_receipts_unknown():
    long_line = launch(ROOT).replace('"pid": null', '"pad": "' + "z" * 70000 + '", "pid": null')
    text = long_line + sweep() + cleanup() + status()
    reader = StatusReader(io.BytesIO(text.encode()))
    reader.run()
    assert reader.dropped and len(reader.lines()) == 3  # the cut line is not kept
    assert _verdict(text)[0].startswith("WORKER_RECEIPTS_UNKNOWN:")


def test_a_worker_record_is_never_overwritten(tmp_path):
    log = tmp_path / "a1.agy.log"
    receipts = _read(good_run(ROOT))
    agy_receipts.write_record(log, receipts, [])
    with pytest.raises(agy_dispatch.AgyWorkerAccountFailed, match="WORKER_RECORD_EXISTS"):
        agy_receipts.write_record(log, receipts, [])
    assert (tmp_path / "a1.agy.worker.json").stat().st_mode & 0o777 == 0o600


def test_lost_lines_still_sweep_the_runs_the_kept_receipts_name(tmp_path, monkeypatch):
    runs = tmp_path / "worker-runs"
    root = runs / RUN_TAG
    root.mkdir(parents=True)
    monkeypatch.setattr(agy_dispatch, "WORKER_RUNS", str(runs))
    swept = []
    monkeypatch.setattr(
        agy_dispatch, "sweep_worker_run", lambda _d, r: swept.append(r) or root.rmdir() or True
    )
    lines = [launch(str(root)).rstrip("\n")]
    receipts = agy_receipts.parse(lines, WORKER, known=False)
    unswept = agy_receipts.sweep_after_kill(None, receipts)
    assert swept == [str(root)] and receipts.post_hoc == {str(root): "clean"}
    assert unswept == ["WORKER_RECEIPTS_UNKNOWN: some dispatcher lines lost"]  # still fails
