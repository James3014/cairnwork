"""A forge-proof verdict for the serializable-I/O interface (b063 report-forgery research, Tier-3).

report_guard.py closes the trivial forgery (a candidate that writes a JUnit file) but says so
honestly: the candidate's code still runs in the same process as the oracle, so a candidate that
reads the nonce, writes a matching record and exits still passes — "partial mitigation, not a
boundary". report_guard's own docstring names the real fix: "the verdict produced outside the
candidate's process (a subprocess per test, or an external observer)".

This module is that external observer, for the case where the thing under test is a function with
JSON-serializable inputs and outputs (the council's "function-level L2"). The candidate is run as
a black box: a trusted harness we wrote imports the candidate's target in a sandboxed child, reads
INPUTS (arguments only) and applies the target to each, and writes RESULTS. The *expected* answers
never leave this (the supervisor) process — not into the child's filesystem, environment, stdin or
the network (Seatbelt denies it). The verdict is computed here by comparing RESULTS to the expected
answers held in memory. So the candidate cannot forge a pass:

  * It can print anything, write anything to RESULTS, or ``os._exit(0)`` early — the supervisor
    reads RESULTS and compares to expected; a missing, short or wrong RESULTS fails the run.
  * It cannot produce correct outputs without correct behaviour, because it never sees the expected
    answers and has no channel to them.

Scope and honesty. This is forge-proof only for a target reachable through a serializable-I/O
interface; stateful, non-deterministic or non-serializable behaviour is out of scope and must fall
back to the ordinary verifier plus the non-technical layer (independent review, live witness, real
usage, A/B). This module is the mechanism only; it is not wired into the pipeline here. Supplying
the holdout cases for a real task (which must be hidden from the candidate — see the "perturbed
oracle" / idea C in the research) is the integration half, owned separately. A pass here is a strong
signal, still bounded by the quality and secrecy of the holdout cases.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

import cairnwork_sandbox
from cairnwork import confined, pytest_config
from cairnwork.confined import RUN_TOKEN_VAR

MAX_RESULT_BYTES = 4 << 20
_TARGET = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")

# The harness is trusted code WE write, run read-only to the child. It carries no expected
# answers: it only applies the candidate's target to inputs and records what came back. A crash,
# an early exit or an exception leaves RESULTS without that index, which the supervisor fails.
_HARNESS_SOURCE = """\
import importlib, json, os

target = os.environ["CAIRNWORK_BBJ_TARGET"]
mod_name, func_name = target.split(":", 1)
inputs = json.load(open(os.environ["CAIRNWORK_BBJ_INPUTS"]))
results_path = os.environ["CAIRNWORK_BBJ_RESULTS"]

module = importlib.import_module(mod_name)
func = getattr(module, func_name)

results = []
for args in inputs:
    try:
        value = func(*args)
        json.dumps(value)  # only a serializable answer counts
        results.append({"ok": True, "value": value})
    except Exception as exc:  # a failing case is data, not a crash
        results.append({"ok": False, "error": type(exc).__name__})
    # written after every case so an early exit still loses only the unanswered tail
    tmp = results_path + ".part"
    with open(tmp, "w") as handle:
        json.dump(results, handle)
    os.replace(tmp, results_path)
"""


class BlackBoxError(Exception):
    """A black-box judge run could not be set up (fail closed; never a silent pass)."""


@dataclass(frozen=True)
class BlackBoxCase:
    """One hidden case. ``expected`` stays in the supervisor and never reaches the candidate.

    ``args`` and ``expected`` must survive a JSON round-trip: the candidate's return value is
    compared after ``json.loads``, so give ``expected`` in that form (lists not tuples, string
    dict keys), or a correct candidate would be failed on a representation mismatch."""

    args: tuple
    expected: object


@dataclass(frozen=True)
class BlackBoxVerdict:
    verified: bool
    executed: bool
    n_correct: int
    n_total: int
    note: str


def run_black_box_judge(
    checkout: Path,
    target: str,
    cases: tuple[BlackBoxCase, ...],
    *,
    python: str,
    timeout: int = 60,
) -> BlackBoxVerdict:
    """Run ``target`` (``module:function``, imported from ``checkout``) on each case as a black box.

    The verdict is computed here from RESULTS vs the expected answers, which never leave this
    process. Fails closed: without a working sandbox, or on any setup error, nothing is accepted.
    """
    if not _TARGET.fullmatch(target):
        raise BlackBoxError(f"target {target!r} must be 'module:function'")
    if not os.path.isabs(python):
        raise BlackBoxError(f"python must be an absolute path, got {python!r}")
    if not cases:
        raise BlackBoxError("no cases given")
    backend = cairnwork_sandbox.select()
    if backend is None:
        return BlackBoxVerdict(False, False, 0, len(cases), "sandbox-exec unavailable")

    private = Path(tempfile.mkdtemp(prefix="bbj.", dir=checkout.parent))
    try:
        return _judge(checkout, target, cases, python, timeout, private, backend)
    finally:
        _scrub(private)


def _judge(checkout, target, cases, python, timeout, private, backend) -> BlackBoxVerdict:
    supervisor = private / "supervisor"  # read-only to the child: harness + inputs only
    run_dir = private / "run"  # the only place besides nothing the child may write
    home = run_dir / "home"
    for directory in (supervisor, run_dir, home):
        directory.mkdir()
    harness = supervisor / "harness.py"
    inputs_path = supervisor / "inputs.json"
    results_path = run_dir / "results.json"
    _write_new(harness, _HARNESS_SOURCE.encode())
    _write_new(inputs_path, json.dumps([list(c.args) for c in cases]).encode())

    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "TMPDIR": str(home),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": str(checkout),  # import the candidate's target from the checkout
        "CAIRNWORK_BBJ_TARGET": target,
        "CAIRNWORK_BBJ_INPUTS": str(inputs_path),
        "CAIRNWORK_BBJ_RESULTS": str(results_path),
        RUN_TOKEN_VAR: secrets.token_hex(16),
    }
    profile = backend.policy(
        read_roots=(*pytest_config.interpreter_roots(python), supervisor, checkout),
        write_roots=(run_dir,),
    )
    profile_path = supervisor / "profile.sb"
    _write_new(profile_path, profile.text.encode())
    # -P (not -I): keep PYTHONPATH so the candidate imports from the checkout, but never put the
    # harness's own directory on sys.path. The environment is already scrubbed to what we set here.
    argv = [python, "-P", str(harness)]
    _returncode, timed_out, survivors = confined.execute(
        backend.wrap(argv, profile_path), checkout, env, timeout, run_dir / "judge.log"
    )
    return _verdict(results_path, cases, timed_out, survivors)


def _verdict(results_path: Path, cases, timed_out: bool, survivors: int) -> BlackBoxVerdict:
    n_total = len(cases)
    if timed_out:
        return BlackBoxVerdict(False, False, 0, n_total, "timed out (possible resource abuse)")
    if survivors:
        # A process that outlived the group kill (e.g. one that escaped its session to tamper with
        # run_dir). It cannot forge a pass, but a clean run leaves none, so do not accept one.
        return BlackBoxVerdict(False, False, 0, n_total, "a process escaped the run")
    results = _read_results(results_path)
    executed = results is not None
    n_correct = 0
    if executed:
        for index, case in enumerate(cases):
            got = results[index] if index < len(results) else None
            if isinstance(got, dict) and got.get("ok") and got.get("value") == case.expected:
                n_correct += 1
    verified = executed and n_correct == n_total
    if verified:
        note = "ok"
    elif not executed:
        note = "no results (crash or early exit)"
    else:
        note = "mismatch"
    return BlackBoxVerdict(verified, executed, n_correct, n_total, note)


def _read_results(results_path: Path) -> list | None:
    """Read RESULTS as a list, or None. The candidate controls this path inside run_dir, so open it
    the way report_guard reads its verdict: O_NOFOLLOW + O_NONBLOCK and a regular-file check, so a
    FIFO or symlink planted in its place cannot hang or redirect the parent. Fail closed on anything
    odd (missing, not a regular file, too large, malformed, not a list)."""
    try:
        fd = os.open(results_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(MAX_RESULT_BYTES + 1)  # cap the read, no stat->read TOCTOU
        if len(raw) > MAX_RESULT_BYTES:
            return None
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, list) else None


def _write_new(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def _scrub(private: Path) -> None:
    import shutil

    shutil.rmtree(private, ignore_errors=True)
