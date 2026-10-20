"""The results reader must fail closed on anything a candidate can plant in run_dir, and must never
hang: a candidate that replaces results.json with a FIFO once the harness stops writing could block
a plain open() forever (independent review finding). These run everywhere (no sandbox needed)."""

import json
import os
import threading

import pytest

from cairnwork.black_box_judge import MAX_RESULT_BYTES, _read_results


def _read_without_hanging(path, timeout=5.0):
    """Call _read_results on another thread so a regression to a blocking open() fails loudly."""
    box = {}
    thread = threading.Thread(target=lambda: box.setdefault("value", _read_results(path)))
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), "_read_results blocked — a planted FIFO can hang the supervisor"
    return box["value"]


def test_regular_file_reads_normally(tmp_path):
    path = tmp_path / "results.json"
    path.write_text(json.dumps([{"ok": True, "value": 2}]))
    assert _read_without_hanging(path) == [{"ok": True, "value": 2}]


def test_fifo_in_place_of_results_does_not_hang_and_is_rejected(tmp_path):
    path = tmp_path / "results.json"
    os.mkfifo(path)
    try:
        assert _read_without_hanging(path) is None  # not a regular file -> None, no hang
    finally:
        path.unlink()


def test_symlink_in_place_of_results_is_rejected(tmp_path):
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps([{"ok": True, "value": 2}]))
    path = tmp_path / "results.json"
    os.symlink(secret, path)
    assert _read_without_hanging(path) is None  # O_NOFOLLOW refuses the symlink


def test_missing_results_is_rejected(tmp_path):
    assert _read_without_hanging(tmp_path / "nope.json") is None


def test_oversize_results_is_rejected(tmp_path):
    path = tmp_path / "results.json"
    path.write_bytes(b"[" + b"0," * (MAX_RESULT_BYTES // 2) + b"0]")
    assert path.stat().st_size > MAX_RESULT_BYTES
    assert _read_without_hanging(path) is None


@pytest.mark.parametrize("blob", [b"not json", b"{}", b'"a string"', b"123"])
def test_non_list_or_malformed_is_rejected(tmp_path, blob):
    path = tmp_path / "results.json"
    path.write_bytes(blob)
    assert _read_without_hanging(path) is None
