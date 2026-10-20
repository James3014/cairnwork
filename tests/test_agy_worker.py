"""The Agy worker's process boundary, with a fake agy binary (no model is called)."""

import json
import os
import sys
from types import SimpleNamespace

from cairnwork.confined import ATTEMPT_ENV, MAX_LOG_BYTES
from cairnwork.workers.agy import AgyWorker

TASK = SimpleNamespace(title="t", instructions="i", allowed_files=("a.py",))


def fake_agy(tmp_path, body):
    path = tmp_path / "agy"
    path.write_text(f"#!{sys.executable}\nimport json, os, sys\n{body}\n")
    path.chmod(0o755)
    return str(path)


def test_agy_gets_only_the_environment_it_needs(tmp_path, monkeypatch):
    monkeypatch.setenv("CW_FAKE_TOKEN", "secret-value")
    dump = tmp_path / "env.json"
    agy = fake_agy(tmp_path, f"open({str(dump)!r}, 'w').write(json.dumps(dict(os.environ)))")
    workdir = tmp_path / "attempt1"
    workdir.mkdir()
    AgyWorker(agy=agy).run(workdir, TASK)
    env = json.loads(dump.read_text())
    assert "CW_FAKE_TOKEN" not in env
    assert env[ATTEMPT_ENV] == "attempt1"
    assert env["HOME"] == os.environ["HOME"]


def test_agy_log_is_capped(tmp_path):
    agy = fake_agy(tmp_path, f"sys.stdout.write('x' * {MAX_LOG_BYTES * 2})")
    workdir = tmp_path / "attempt2"
    workdir.mkdir()
    AgyWorker(agy=agy).run(workdir, TASK)
    assert (tmp_path / "attempt2.agy.log").stat().st_size == MAX_LOG_BYTES + 1
