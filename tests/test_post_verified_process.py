"""The post-verified hook's process and log handling: timeouts, odd output, unsafe logs."""

import json
import os
import subprocess
import sys
import time

import pytest
from test_post_verified import _hook

import cairnwork.intake_config as intake_config
import cairnwork.post_verified as post_verified


def _direct(tmp_path, body="exit 0", seconds=300):
    hook, seen = _hook(tmp_path, body)
    (tmp_path / "state").mkdir(exist_ok=True)
    cfg = {**intake_config.DEFAULT_CONFIG, "post_verified_hook": hook,
           "post_verified_hook_seconds": seconds}  # fmt: skip

    def run():
        done = post_verified.run_post_verified_hook(
            tmp_path / "state", cfg, task_id="t1", attempt_id="a1", candidate="c",
            clone=tmp_path, repo_slug="o/r", gh="/bin/gh",
        )  # fmt: skip
        assert done is not None
        return done

    return run, tmp_path / "state" / post_verified.LOG_NAME, seen


def test_a_fifo_log_does_not_block_and_is_alerted(tmp_path, capsys):
    run, log, _ = _direct(tmp_path)
    os.mkfifo(log)
    started = time.monotonic()
    assert run()["ok"] is True
    assert time.monotonic() - started < 10 and "cannot record" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["readable", "hardlink"])
def test_an_existing_log_that_is_not_private_is_not_written(tmp_path, capsys, kind):
    run, log, _ = _direct(tmp_path)
    if kind == "readable":
        log.write_text("old\n")
        log.chmod(0o644)
    else:
        other = tmp_path / "other"
        other.write_text("old\n")
        other.chmod(0o600)
        os.link(other, log)
    assert run()["ok"] is True
    assert log.read_text() == "old\n" and "cannot record" in capsys.readouterr().err


def test_an_existing_private_log_is_appended_to(tmp_path):
    run, log, _ = _direct(tmp_path)
    run()
    run()
    assert len(log.read_text().splitlines()) == 2


def _gone(pid, within=10):
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    os.kill(pid, 9)
    return False


def test_a_timeout_also_kills_what_the_hook_started(tmp_path):
    # the child does not hold the output pipes, so only the group kill can end it in time
    body = f"sleep 600 >/dev/null 2>&1 & echo $! > {tmp_path}/child; wait"
    run, _log_path, _seen = _direct(tmp_path, body, 1)
    assert run()["reason"] == "TIMEOUT"
    assert _gone(int((tmp_path / "child").read_text())), "the hook's child survived the timeout"


def test_a_detached_descendant_cannot_hold_settlement(tmp_path, monkeypatch):
    monkeypatch.setattr(post_verified, "CLEANUP_SECONDS", 1)
    daemon = (
        f'python3 -c \'import os,time; os.setsid(); open("{tmp_path}/child","w")'
        f".write(str(os.getpid())); time.sleep(600)' &\nwait"
    )
    run, _log_path, _seen = _direct(tmp_path, daemon, 1)
    started = time.monotonic()
    try:
        assert run()["reason"] == "TIMEOUT" and time.monotonic() - started < 10
    finally:
        os.kill(int((tmp_path / "child").read_text()), 9)


def test_output_that_is_not_utf8_is_still_recorded(tmp_path):
    run, log, _seen = _direct(tmp_path, r"printf '\377\376'; exit 3")
    assert run()["reason"] == "exit 3"
    (line,) = [json.loads(x) for x in log.read_text().splitlines()]
    assert line["returncode"] == 3 and "\ufffd" in line["stdout_tail"]


def test_a_broken_stderr_pipe_does_not_change_the_exit_code(tmp_path):
    read, write = os.pipe()
    os.close(read)  # nobody will ever read the alert
    code = "from cairnwork.post_verified import alert; alert('x' * 100)"
    done = subprocess.run([sys.executable, "-c", code], stderr=write, check=False, timeout=60)
    os.close(write)
    assert done.returncode == 0
