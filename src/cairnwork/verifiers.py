"""Run a task's verifiers on a fresh checkout of the candidate (preserved behaviors #5, #14).

Verifiers come from the task, so they are untrusted text. Only one shape is
accepted: ``python3 -m pytest [-q] [-p no:cacheprovider] tests/<file>.py[::<name>]...``.
Anything else is rejected before it runs, and nothing goes through a shell.

Each run happens in a new clone of the candidate commit made from Cairnwork's
repository (never in the worker's clone), with a minimal environment (no
GIT_*, no inherited credentials, HOME and TMPDIR inside the run directory,
bytecode and the pytest cache disabled) and a timeout. A verifier passes only
if it exits 0 and leaves the checkout identical to the candidate tree; a
verifier that writes into the checkout fails. The pytest run must also leave a
JUnit report showing at least one test run and none failed, errored, skipped or
xfailed. pytest's configuration is pinned to a Cairnwork-owned empty ini with
rootdir and confcutdir at the checkout, so no ini in the candidate or a parent
directory applies, and plugin autoloading is off. Output streams to a log file
with a size cap, the run's process group is killed afterwards, and logs and
reports live in a private directory, created with O_EXCL|O_NOFOLLOW. A process
that starts its own session escapes the group kill; only a sandbox stops that.
The observation hash binds the candidate commit, verifier id, full argv, exit
code, timeout, mutations, test result and log hash.

The candidate's code runs on this host and in the same process as pytest. This
is not a sandbox: hostile code can reach the network and the user's files, and
can forge a passing result (for example by writing the report and exiting).
Python runs with ``-P`` so the checkout is never on ``sys.path`` before the real
pytest (a root ``pytest.py`` cannot replace it). Callers should keep tests,
pytest configuration and root-level modules out of the files a candidate may
change (``protected_verifier_inputs``), but that is not enough on its own:
candidate source code also runs inside the pytest process and can forge a
result. A pass here is evidence, not proof, until verifiers run in a sandbox. Running verifiers in a
sandbox is tracked in docs/PRESERVED_BEHAVIORS.md (open question under #14).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import resource
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from cairnwork.core_request import VerifierObservation
from cairnwork.treecompare import tree_differences
from cairnwork.workspace import WorkspaceError, create_attempt_clone, remove_attempt_clone

DEFAULT_TIMEOUT_SECONDS = 600
_TARGET = re.compile(r"^tests/(?:[A-Za-z0-9_\-]+/)*[A-Za-z0-9_\-]+\.py(?:::[A-Za-z0-9_\[\]\-]+)*$")
_OPTIONS = {("-q",), ("-p", "no:cacheprovider")}
_ENV_KEEP = ("PATH", "LANG", "LC_ALL")
_PROTECTED_NAMES = frozenset(
    {
        "conftest.py",
        "pytest.ini",
        "pyproject.toml",
        "setup.cfg",
        "tox.ini",
        "setup.py",
        "sitecustomize.py",
        "usercustomize.py",
    }
)
MAX_LOG_BYTES = 4 << 20
MAX_REPORT_BYTES = 4 << 20


class VerifierRejected(Exception):
    code = "VERIFIER_NOT_ALLOWED"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


@dataclass(frozen=True)
class VerifierSpec:
    verifier_id: str
    targets: tuple[str, ...]


@dataclass(frozen=True)
class VerifierRun:
    spec: VerifierSpec
    returncode: int | None
    timed_out: bool
    mutated: tuple[str, ...]
    log_path: Path
    observation: VerifierObservation


def parse_verifier(verifier_id: str, argv: list[str]) -> VerifierSpec:
    """Accept only python3 -m pytest with allowed options and tests/*.py targets."""
    if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
        raise VerifierRejected("argv must be a list of strings")
    if argv[:3] != ["python3", "-m", "pytest"]:
        raise VerifierRejected(f"{verifier_id}: only 'python3 -m pytest' is allowed")
    rest, targets = argv[3:], []
    while rest:
        if tuple(rest[:1]) in _OPTIONS:
            rest = rest[1:]
        elif tuple(rest[:2]) in _OPTIONS:
            rest = rest[2:]
        elif _TARGET.match(rest[0]):
            targets.append(rest.pop(0))
        else:
            raise VerifierRejected(f"{verifier_id}: argument {rest[0]!r} is not allowed")
    if not targets:
        raise VerifierRejected(f"{verifier_id}: no tests/*.py target")
    return VerifierSpec(verifier_id, tuple(targets))


def run_verifiers(
    repo: Path,
    candidate_commit: str,
    specs: tuple[VerifierSpec, ...],
    run_root: Path,
    run_id: str,
    *,
    python: str = sys.executable,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[VerifierRun, ...]:
    """Run each spec on a fresh checkout of candidate_commit; a failing test is not an error.

    If a checkout cannot be created the error propagates and no observation is
    returned (fail closed).
    """
    ids = [spec.verifier_id for spec in specs]
    if len(ids) != len(set(ids)):
        raise VerifierRejected(f"duplicate verifier ids in {ids}")
    runs = []
    for index, spec in enumerate(specs):
        attempt_id = f"{run_id}-v{index}"
        checkout = create_attempt_clone(repo, candidate_commit, run_root, attempt_id).path
        try:
            runs.append(_run_one(repo, candidate_commit, spec, checkout, python, timeout))

        finally:
            try:
                remove_attempt_clone(run_root, attempt_id)
            except WorkspaceError:
                pass  # the run's verdict stands; a leftover directory is only disk space
    return tuple(runs)


def protected_verifier_inputs(paths: tuple[str, ...]) -> tuple[str, ...]:
    """Paths a candidate must not change, because they decide what the verifier runs."""
    return tuple(
        path
        for path in paths
        if path.startswith("tests/")
        or path.rsplit("/", 1)[-1] in _PROTECTED_NAMES
        or ("/" not in path and path.endswith((".py", ".pth")))
    )


def _run_one(repo, candidate_commit, spec, checkout, python, timeout) -> VerifierRun:
    private = Path(tempfile.mkdtemp(prefix=f"{checkout.name}.", dir=checkout.parent))
    report, log_path, ini = private / "junit.xml", private / "verifier.log", private / "pytest.ini"
    _write_new(ini, b"[pytest]\n")
    env = {key: os.environ[key] for key in _ENV_KEEP if key in os.environ}
    env.update(
        HOME=str(private),
        TMPDIR=str(private),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONNOUSERSITE="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
    )
    argv = [
        python,
        "-P",
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "-c",
        str(ini),
        f"--rootdir={checkout}",
        f"--confcutdir={checkout}",
        "-o",
        "addopts=",
        f"--junitxml={report}",
        *spec.targets,
    ]
    returncode, timed_out = _execute(argv, checkout, env, timeout, log_path)
    mutated = tuple(tree_differences(repo, candidate_commit, checkout))
    ran = _report_shows_passing_tests(report)
    too_big = log_path.stat().st_size > MAX_LOG_BYTES
    passed = returncode == 0 and not timed_out and not mutated and ran and not too_big
    record = json.dumps(
        {
            "candidate_commit": candidate_commit,
            "verifier_id": spec.verifier_id,
            "argv": argv,
            "returncode": returncode,
            "timed_out": timed_out,
            "mutated": mutated,
            "tests_passed": ran,
            "log_sha256": _file_sha256(log_path),
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(record.encode()).hexdigest()
    observation = VerifierObservation(
        spec.verifier_id, f"{private.name}/verifier.log", f"sha256:{digest}", passed
    )
    return VerifierRun(spec, returncode, timed_out, mutated, log_path, observation)


def _execute(argv, cwd, env, timeout, log_path) -> tuple[int | None, bool]:
    """Run in a new process group with output streamed to log_path; kill the group after."""
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as log:
        process = subprocess.Popen(  # noqa: S603 - argv built from an allow-listed spec, no shell
            argv,
            cwd=cwd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            preexec_fn=_limit_file_size,  # noqa: PLW1509 - only setrlimit, no locks
        )
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        _kill_group(process.pid)
        process.wait()
    return (None if timed_out else process.returncode), timed_out


def _limit_file_size() -> None:
    """In the child: no file it writes (log, report, scratch) may exceed the caps."""
    limit = MAX_LOG_BYTES + MAX_REPORT_BYTES
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass  # the group is already gone


def _report_shows_passing_tests(report: Path) -> bool:
    """The JUnit report must exist and show at least one test run, none failed or skipped all."""
    try:
        fd = os.open(report, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(MAX_REPORT_BYTES + 1)
        if len(data) > MAX_REPORT_BYTES:
            return False
        root = ElementTree.fromstring(data)  # noqa: S314 - size-capped, our own report path
    except (OSError, ElementTree.ParseError):
        return False
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    totals = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
    try:
        for suite in suites:
            for key in totals:
                totals[key] += int(suite.get(key, "0"))
    except ValueError:
        return False
    return (
        totals["tests"] > 0
        and totals["failures"] == 0
        and totals["errors"] == 0
        and totals["skipped"] == 0
    )


def _write_new(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
