"""Process-list probes ask again before saying "cannot tell" (Issue #114).

On a loaded host ``ps`` or ``sudo kill`` can exceed a short timeout once and answer at the next
try. A single timeout used to read as a survivor, so a run whose tests passed was recorded FAIL.
"""

import subprocess
import sys
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


def test_process_environments_stay_unknown_after_a_timeout(monkeypatch):
    if sys.platform.startswith("linux"):
        pytest.skip("Linux reads /proc, not ps")
    calls = _flaky(monkeypatch, confined, 99, None)
    assert confined._processes_with_environment() is None  # fail closed, no second look
    assert len(calls) == 1
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
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="")  # ps: nothing left

    monkeypatch.setattr(va.subprocess, "run", fake)
    assert account.kill_all() == 0 and len(kills) == 2


def _fake_account(ps_before, ps_after):
    """ps answers (stdout or an exception) in order; every kill succeeds."""
    answers = [ps_before, *ps_after]
    seen = {"killed": False, "ps_before_kill": 0}

    def fake(argv, **kwargs):
        if "/bin/kill" in argv:
            seen["killed"] = True
            return SimpleNamespace(returncode=-9, stdout="", stderr="")
        if not seen["killed"]:
            seen["ps_before_kill"] += 1
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(returncode=0 if answer else 1, stdout=answer, stderr="")

    return fake, seen


def test_an_escaped_process_that_exits_during_the_retry_window_still_counts(monkeypatch):
    """ps saw a process before the kill; after the kill it is gone. It still counts (kills a
    'reap ignores what was seen before' mutant)."""
    fake, _ = _fake_account("777\n", [""])
    monkeypatch.setattr(va.subprocess, "run", fake)
    assert va.VerifierAccount(user="nobody").reap() == 1


def test_a_reap_with_nothing_running_is_clean(monkeypatch):
    fake, _ = _fake_account("", [""])
    monkeypatch.setattr(va.subprocess, "run", fake)
    assert va.VerifierAccount(user="nobody").reap() == 0


def test_an_unreadable_list_before_the_kill_is_not_retried_and_counts(monkeypatch):
    """The pre-kill read is asked once: a timeout means unknown (survivor), and ps is only asked
    again after the kill, so a live escapee gets no retry window before it is killed."""
    fake, seen = _fake_account(subprocess.TimeoutExpired("ps", 1), [""])
    monkeypatch.setattr(va.subprocess, "run", fake)
    assert va.VerifierAccount(user="nobody").reap() == 1
    assert seen["ps_before_kill"] == 1


def test_the_probe_deadline_fails_closed(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(va.time, "monotonic", lambda: clock["now"])

    def slow(argv, **kwargs):
        clock["now"] += kwargs["timeout"]
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(va.subprocess, "run", slow)
    assert va.VerifierAccount(user="nobody").reap() >= 1
    assert clock["now"] <= va.PROBE_DEADLINE_SECONDS


def test_mutated_names_are_counted_cut_and_cleaned():
    base: dict[str, Any] = dict(
        ran_sandboxed=True, returncode=0, timed_out=False, survivors=0,
        log_too_big=False, tests_ran=True,
    )  # fmt: skip
    names = tuple(f"f{i}\n" + "x" * 500 for i in range(9))
    reason = why_not_passed(**base, mutated=names)
    assert reason.startswith("checkout changed (9: ") and "+4 more" in reason
    assert "\n" not in reason and reason.count("f") <= 5 + 2
    assert len(reason) < 5 * 125 + 100


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
