"""Process-list probes ask again before saying "cannot tell" (Issue #114).

On a loaded host ``ps`` or ``sudo kill`` can exceed a short timeout once and answer at the next
try. A single timeout used to read as a survivor, so a run whose tests passed was recorded FAIL.
"""

import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

import cairnwork.confined as confined
import cairnwork.verifier_account as va
from cairnwork.core_request import VerifierObservation
from cairnwork.reasons import explain, why_not_passed


def _flaky(monkeypatch, module, fails, answer):
    calls = []

    def fake(argv, **kwargs):
        calls.append(argv)
        if len(calls) <= fails:
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))
        return answer

    monkeypatch.setattr(module.subprocess, "run", fake)
    return calls


def test_process_environments_are_read_after_a_timeout(monkeypatch):
    ok = SimpleNamespace(returncode=0, stdout="12 12 A=1 B=2\n", stderr="")
    calls = _flaky(monkeypatch, confined, 2, ok)
    assert confined._processes_with_environment() == [(12, 12, {"A=1", "B=2"})]
    assert len(calls) == 3


def test_process_environments_stay_unknown_when_every_try_times_out(monkeypatch):
    calls = _flaky(monkeypatch, confined, 99, None)
    assert confined._processes_with_environment() is None  # fail closed
    assert len(calls) == confined.PS_ATTEMPTS
    assert confined._sweep_survivors("TOKEN", 1) == 1


def test_account_count_is_read_after_a_timeout(monkeypatch):
    ok = SimpleNamespace(returncode=0, stdout="11\n12\n", stderr="")
    calls = _flaky(monkeypatch, va, 2, ok)
    assert va.VerifierAccount(user="nobody").count() == 2
    assert len(calls) == 3


def test_account_count_stays_unknown_when_every_try_times_out(monkeypatch):
    calls = _flaky(monkeypatch, va, 99, None)
    assert va.VerifierAccount(user="nobody").count() == -1
    assert len(calls) == confined.PS_ATTEMPTS


def test_a_timed_out_kill_is_tried_again(monkeypatch):
    account = va.VerifierAccount(user="nobody")
    kills = []

    def fake(argv, **kwargs):
        if "/bin/kill" in argv:
            kills.append(argv)
            if len(kills) == 1:
                raise subprocess.TimeoutExpired(argv, 1)
            return None
        return SimpleNamespace(returncode=1, stdout="", stderr="")  # ps: nothing left

    monkeypatch.setattr(va.subprocess, "run", fake)
    assert account.kill_all() == 0 and len(kills) == 2


def test_every_cause_is_named():
    base: dict[str, Any] = dict(
        ran_sandboxed=True, returncode=0, timed_out=False, mutated=(), survivors=0,
        log_too_big=False, tests_ran=True,
    )  # fmt: skip
    assert why_not_passed(**base) == ""
    assert "no sandbox" in why_not_passed(**{**base, "ran_sandboxed": False})
    assert "timed out" in why_not_passed(**{**base, "timed_out": True, "returncode": None})
    assert "a.py" in why_not_passed(**{**base, "mutated": ("a.py",)})
    assert "2 process" in why_not_passed(**{**base, "survivors": 2})
    assert "size cap" in why_not_passed(**{**base, "log_too_big": True})
    assert "exit code 1" in why_not_passed(**{**base, "returncode": 1})
    assert "no test run" in why_not_passed(**{**base, "tests_ran": False})


def _run(verifier_id, passed, why):
    observation = VerifierObservation(verifier_id, "a", "h", passed)
    return SimpleNamespace(
        spec=SimpleNamespace(verifier_id=verifier_id), observation=observation, why=why
    )


def test_explain_names_only_the_failed_verifiers():
    runs = [_run("v1", True, ""), _run("v2", False, "timed out"), _run("v3", False, "")]
    assert explain(runs) == " [v2: timed out; v3: no cause recorded]"
    assert explain([_run("v1", True, "")]) == ""


@pytest.mark.parametrize("attempts", [confined.PS_ATTEMPTS])
def test_retry_is_bounded(attempts):
    assert 2 <= attempts <= 5
