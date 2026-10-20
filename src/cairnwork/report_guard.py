"""Trusted verdict for a verifier run (preserved behavior #14; b063 report-forgery research).

The pass/fail comes from a pytest plugin that records what pytest itself collected and how
each test ended, written once at ``sessionfinish`` — not from a JUnit file the candidate can
write. verifiers.py used to pass ``--junitxml=<path>`` in argv and read that file, but the
candidate's own code runs in the same process and could read the path from ``sys.argv``, write
a passing report and ``os._exit(0)`` before the real tests ran. The parent now accepts a run
only when our record is present (so an exit before ``sessionfinish`` fails), it names at least
one collected test, every collected test has a result, and all of them passed.

The plugin is a small standalone module (``_PLUGIN_SOURCE``) materialised into a per-run
directory by ``install_plugin`` and loaded with ``-p``. It carries no cairnwork import, so it
loads even when cairnwork is not importable from inside the sandbox (an editable checkout under
a 0700 home the verifier account cannot traverse); that directory is read-only to the sandbox,
so the candidate cannot replace it.

Limitation: the plugin runs in the same process as the candidate, so a candidate that reads the
nonce from the environment could still write a forged record to the verdict file before exiting.
Closing that needs a verdict channel outside the process (a subprocess per test, or an external
observer); until then this raises the cost of forgery from "write the file named in argv" to
"forge the plugin's record", and defeats the two demonstrated attacks (a forged JUnit and
``os._exit``).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from cairnwork.confined import MAX_REPORT_BYTES

PATH_ENV = "CAIRNWORK_VERDICT_PATH"
NONCE_ENV = "CAIRNWORK_VERDICT_NONCE"
MODULE = "_cairnwork_report_guard"

_PLUGIN_SOURCE = '''\
"""Materialised by cairnwork.report_guard into each verifier run; edit it there."""

import json
import os

_PATH_ENV = "CAIRNWORK_VERDICT_PATH"
_NONCE_ENV = "CAIRNWORK_VERDICT_NONCE"


class _Reporter:
    def __init__(self, path, nonce):
        self._path = path
        self._nonce = nonce
        self._collected = []
        self._outcomes = {}

    def pytest_collection_finish(self, session):
        self._collected = [item.nodeid for item in session.items]

    def pytest_runtest_logreport(self, report):
        if report.when == "call":
            self._outcomes[report.nodeid] = report.outcome
        elif report.when == "setup" and report.outcome in ("skipped", "failed"):
            self._outcomes.setdefault(report.nodeid, report.outcome)

    def pytest_sessionfinish(self, session, exitstatus):
        payload = {"nonce": self._nonce, "collected": self._collected, "outcomes": self._outcomes}
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle)
        os.chmod(self._path, 0o644)  # the owner reads it (a different user under the account)


def pytest_configure(config):
    path, nonce = os.environ.get(_PATH_ENV), os.environ.get(_NONCE_ENV)
    if path and nonce:
        config.pluginmanager.register(_Reporter(path, nonce), "cairnwork_report_guard")
'''


def install_plugin(directory: Path) -> None:
    """Write the standalone plugin into directory (which must be on the run's PYTHONPATH and a
    sandbox read root, and must not be writable by the candidate)."""
    path = directory / f"{MODULE}.py"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    with os.fdopen(fd, "w") as handle:
        handle.write(_PLUGIN_SOURCE)
    os.chmod(path, 0o640)  # not the umask-masked open() mode: the verifier account reads by group


@dataclass(frozen=True)
class Verdict:
    ran: bool
    outcomes: dict[str, str]


def read_verdict(path: Path, nonce: str) -> Verdict:
    """Read the plugin's record. ``ran`` is true only when sessionfinish wrote it, it matches
    the nonce, at least one test was collected, every collected test produced a result, and all
    results are ``passed``. Outcomes are returned whenever the nonce matches (even when not all
    passed) so failed_on_base can see a test that failed."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_REPORT_BYTES + 1)  # a candidate-writable file; cap the read
        if len(raw) > MAX_REPORT_BYTES:
            return Verdict(False, {})
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError, ValueError):
        return Verdict(False, {})
    if not isinstance(data, dict) or data.get("nonce") != nonce:
        return Verdict(False, {})
    collected = set(data.get("collected") or [])
    outcomes = data.get("outcomes") or {}
    if not isinstance(outcomes, dict):
        return Verdict(False, {})
    ran = (
        bool(collected)
        and set(outcomes) == collected
        and all(outcome == "passed" for outcome in outcomes.values())
    )
    return Verdict(ran, outcomes)
