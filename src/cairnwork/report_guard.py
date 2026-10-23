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

What this does and does not do. It removes the old trivial attack (read ``--junitxml`` from
argv, write a passing report) and makes the verdict reflect what pytest itself collected and
ran. It defeats a candidate that only writes a forged JUnit (now ignored) or only calls
``os._exit`` without writing a record. It does NOT stop a candidate that reads the nonce and
path from the environment, writes a matching record and exits — reading the nonce from the
environment is no harder than reading the old argv path, so this is partial mitigation, not a
boundary.

Raising the bar (this module, ``report_channel`` and ``tamper_scan``): when the verifier runs
as the owner (no verifier account) the nonce and the report destination no longer sit in the
environment. The parent hands the child two inherited file descriptors (``pass_fds``): a pipe
holding the nonce and an anonymous (already unlinked) file for the record; only their numbers
travel in ``CAIRNWORK_GUARD_FDS``. The plugin reads the nonce, closes that pipe and removes every
guard variable from ``os.environ`` when its module is imported, which is before any conftest or
candidate code. The parent reads the record from its own descriptor, so there is no path to
guess, replace or plant. Candidate patches are also scanned for the APIs such a forgery needs
(tamper_scan.py, reason ``VERIFIER_TAMPER_API``). Both are a higher hurdle, not a boundary: a
process that shares the interpreter can still scan its open descriptors or build the API names
from strings. Under a verifier account sudo closes descriptors above 2, so that path keeps the
environment hand-over (path and nonce), still scrubbed by the plugin once read.

A real boundary needs the verdict produced outside the candidate's process (a subprocess per
test, or an external observer), tracked as Tier-3 in the report-forgery research
(knowledge nexus/research/2026-10-20-forgeable-test-report.md).
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from cairnwork.confined import MAX_REPORT_BYTES

FDS_ENV = "CAIRNWORK_GUARD_FDS"
PATH_ENV = "CAIRNWORK_VERDICT_PATH"
NONCE_ENV = "CAIRNWORK_VERDICT_NONCE"
MODULE = "_cairnwork_report_guard"

_PLUGIN_SOURCE = '''\
"""Materialised by cairnwork.report_guard into each verifier run; edit it there."""

import json
import os

_FDS_ENV = "CAIRNWORK_GUARD_FDS"
_PATH_ENV = "CAIRNWORK_VERDICT_PATH"
_NONCE_ENV = "CAIRNWORK_VERDICT_NONCE"


def _take():
    """Read the hand-over once and scrub it from os.environ. Runs at import: pytest loads
    this module (-p) before any conftest or candidate code."""
    spec = os.environ.pop(_FDS_ENV, None)
    path = os.environ.pop(_PATH_ENV, None)
    nonce = os.environ.pop(_NONCE_ENV, None)
    if spec:
        try:
            nonce_fd, report_fd = (int(part) for part in spec.split(","))
            chunks = []
            while True:
                data = os.read(nonce_fd, 4096)
                if not data:
                    break
                chunks.append(data)
            os.close(nonce_fd)
            return None, report_fd, b"".join(chunks).decode()
        except (OSError, ValueError):
            return None
    return (path, None, nonce) if path and nonce else None


_HANDOVER = _take()


class _Reporter:
    def __init__(self, path, report_fd, nonce):
        self._path = path
        self._fd = report_fd
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
        if self._fd is not None:
            data = json.dumps(payload).encode()
            os.ftruncate(self._fd, 0)
            written = 0
            while written < len(data):
                written += os.pwrite(self._fd, data[written:], written)
            return
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle)
        os.chmod(self._path, 0o644)  # the owner reads it (a different user under the account)


def pytest_configure(config):
    global _HANDOVER
    if _HANDOVER:
        config.pluginmanager.register(_Reporter(*_HANDOVER), "cairnwork_report_guard")
        _HANDOVER = None
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
    too_big: bool = False  # the file exceeds MAX_REPORT_BYTES: nothing could be read from it


def read_verdict(path: Path, nonce: str) -> Verdict:
    """Read the plugin's record from a path (the verifier-account hand-over). See
    ``parse_verdict`` for what makes ``ran`` true."""
    try:
        # a candidate controls this path in run_dir: no symlink, and O_NONBLOCK + a regular-file
        # check so a FIFO planted in its place cannot hang the parent open().
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return Verdict(False, {})
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(MAX_REPORT_BYTES + 1)  # cap the read
    except OSError:
        return Verdict(False, {})
    return parse_verdict(raw, nonce)


def read_verdict_fd(fd: int, nonce: str) -> Verdict:
    """Read the record from the parent's own descriptor of the anonymous report file."""
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return Verdict(False, {})
        raw = os.pread(fd, MAX_REPORT_BYTES + 1, 0)
    except OSError:
        return Verdict(False, {})
    return parse_verdict(raw, nonce)


def parse_verdict(raw: bytes, nonce: str) -> Verdict:
    """``ran`` is true only when sessionfinish wrote the record, it matches
    the nonce, at least one test was collected, every collected test produced a result, and all
    results are ``passed``. Outcomes are returned whenever the nonce matches (even when not all
    passed) so failed_on_base can see a test that failed. A record over MAX_REPORT_BYTES (a suite
    too large for one report, #184) is ``too_big`` so the caller can say why nothing was read."""
    if len(raw) > MAX_REPORT_BYTES:
        return Verdict(False, {}, too_big=True)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return Verdict(False, {})
    if not isinstance(data, dict) or data.get("nonce") != nonce:
        return Verdict(False, {})
    collected = data.get("collected")
    outcomes = data.get("outcomes")
    # fail closed on any malformed shape (a candidate controls the file): node ids and outcomes
    # must be strings, else set()/membership/str compares could misbehave or crash the parent.
    if not isinstance(collected, list) or not all(isinstance(node, str) for node in collected):
        return Verdict(False, {})
    if not isinstance(outcomes, dict) or not all(
        isinstance(node, str) and isinstance(result, str) for node, result in outcomes.items()
    ):
        return Verdict(False, {})
    ran = (
        bool(collected)
        and set(outcomes) == set(collected)
        and all(outcome == "passed" for outcome in outcomes.values())
    )
    return Verdict(ran, outcomes)
