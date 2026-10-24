"""scripts/preflight.py (#234): one JSON object, a code per check, exit 0 only when all are OK,
and nothing written to the state."""

import json
import os
import pwd
import sys
from contextlib import contextmanager

import pytest
from conftest import git

from cairnwork.intake import Intake
from scripts import preflight


@pytest.fixture
def mirror(repo, tmp_path):
    path = (tmp_path / "mirror").resolve()
    git(tmp_path, "clone", "-q", "--single-branch", "--branch", "main", str(repo), str(path))
    return path


def _state(tmp_path, **config):
    state = tmp_path / "state"
    state.mkdir(mode=0o700, exist_ok=True)
    (state / "config.json").write_text(json.dumps({"verifier_account": False, **config}))
    return state


def _run(state, *extra, capsys):
    code = preflight.main(["--state", str(state), *extra])
    return code, json.loads(capsys.readouterr().out)


def _codes(report):
    return {name: check["code"] for name, check in report["checks"].items()}


def test_a_healthy_state_is_ok_and_untouched(mirror, tmp_path, capsys):
    state = _state(tmp_path, mirror=str(mirror), verifier_python=sys.executable)
    before = sorted(p.name for p in state.iterdir())
    code, report = _run(state, "--clone", str(mirror), capsys=capsys)
    assert code == 0 and report["ok"] is True, report
    assert set(_codes(report).values()) == {"OK"}
    assert sorted(p.name for p in state.iterdir()) == before  # read-only


def test_a_clone_that_is_not_the_mirror_fails_the_run(mirror, tmp_path, capsys):
    state = _state(tmp_path, mirror=str(mirror))
    code, report = _run(state, "--clone", str(tmp_path), capsys=capsys)
    assert code == 1 and report["ok"] is False
    assert _codes(report)["clone"] == "CLONE_NOT_STATE_MIRROR"
    assert _codes(report)["mirror"] == "OK"


def test_missing_python_and_mirror_have_their_own_codes(tmp_path, capsys):
    state = _state(tmp_path, mirror=str(tmp_path / "gone"), verifier_python="/nope/python")
    code, report = _run(state, capsys=capsys)
    assert code == 1 and _codes(report)["mirror"] == "MIRROR_MISSING"
    assert _codes(report)["verifier_python"] == "VERIFIER_PYTHON_MISSING"


def test_a_bad_config_or_a_missing_state_skips_the_rest(tmp_path, capsys):
    state = _state(tmp_path, nonsense=1)
    code, report = _run(state, capsys=capsys)
    assert code == 1 and _codes(report)["config"] == "BAD_CONFIG"
    assert {c for n, c in _codes(report).items() if n != "config"} == {"SKIPPED"}
    code, report = _run(tmp_path / "nowhere", capsys=capsys)
    assert code == 1 and _codes(report)["config"] == "STATE_MISSING"


class Unusable:
    user = "cairnwork-verifier"
    python = "/Users/nobody-here/venv/bin/python"

    @contextmanager
    def exclusive(self, timeout=None):
        yield

    def available(self, python=None):
        return False


def test_an_unusable_account_says_where_it_is_blocked(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(Intake, "verifier_account", lambda self: Unusable())
    code, report = _run(_state(tmp_path), capsys=capsys)
    account = report["checks"]["account"]
    assert code == 1 and account["code"] == "VERIFIER_ACCOUNT_UNUSABLE"
    assert "cannot run /Users/nobody-here/venv/bin/python" in account["detail"]
    assert isinstance(account["walk"], list)


def test_a_run_as_user_that_is_the_owner_is_not_isolated(tmp_path, capsys):
    owner = pwd.getpwuid(os.getuid()).pw_name
    state = _state(tmp_path, agy_dispatch="/bin/d", agy_run_as_user=owner)
    code, report = _run(state, capsys=capsys)
    assert code == 1 and _codes(report)["worker_isolation"] == "WORKER_NOT_ISOLATED"
    assert "root or the owner" in report["checks"]["worker_isolation"]["detail"]
