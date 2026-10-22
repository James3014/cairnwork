"""Seatbelt POSIX-semaphore rule selection: inconclusive probes fail closed (#116)."""

import pytest

REGEX_RULE = "R"


@pytest.mark.parametrize(
    ("baseline", "regex", "expected"),
    [
        (0, 0, REGEX_RULE),
        (0, 65, "(allow ipc-posix-sem)"),  # confirmed rejection of the regex form
        (0, None, ""),  # regex probe timed out / could not run: fail closed
        (None, None, ""),
        (None, 65, ""),  # baseline did not run: the non-zero is not evidence of rejection
        (1, 65, ""),
    ],
)
def test_posix_sem_rule_fails_closed_on_inconclusive_probes(baseline, regex, expected):
    from cairnwork_sandbox import seatbelt

    assert seatbelt._sem_rule_from_probes(REGEX_RULE, baseline, regex) == expected


def test_posix_sem_rule_never_broadens_when_probe_returns_none(monkeypatch):
    from cairnwork_sandbox import seatbelt

    seatbelt.posix_sem_rule.cache_clear()
    monkeypatch.setattr(seatbelt, "_probe", lambda policy: None)
    monkeypatch.setattr(seatbelt.os, "access", lambda *a, **k: True)
    try:
        assert seatbelt.posix_sem_rule() == ""
    finally:
        seatbelt.posix_sem_rule.cache_clear()
