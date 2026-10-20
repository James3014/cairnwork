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

The run is confined with macOS Seatbelt (see sandbox.py): no network, reads only
from the system, the interpreter and the checkout, writes only to the checkout
and a per-run directory. Without a working sandbox-exec nothing runs and the
verifier fails (no unconfined fallback). Logs, the pytest ini and the profile
live outside what the child may write. The candidate's code still runs in the
same process as pytest, so it can forge the report inside the per-run
directory; a pass is evidence, not proof.

Known gap: Seatbelt does not stop a sandboxed process from reading other
processes' arguments and environment (sysctl KERN_PROCARGS2) when they run as
the same user, so tokens in other processes' environments are readable and could
be printed into the verifier log. A VerifierAccount closes that (another uid);
any process the account still runs after a verifier is killed and fails the run,
and none may run before the next one. Without one, a process that leaves the
run's process group is killed only if it still carries the run token in its
environment. Either way, never pass raw verifier logs to a worker or a model.
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
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from xml.etree import ElementTree

from cairnwork import confined, sandbox
from cairnwork.confined import ATTEMPT_ENV, MAX_LOG_BYTES, MAX_REPORT_BYTES, RUN_TOKEN_VAR
from cairnwork.core_request import VerifierObservation
from cairnwork.treecompare import tree_differences
from cairnwork.verifier_account import VerifierAccount, VerifierAccountError
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
    attempt_id: str = "",
    account: VerifierAccount | None = None,
) -> tuple[VerifierRun, ...]:
    """Run each spec on a fresh checkout of candidate_commit; a failing test is not an error.

    If a checkout cannot be created the error propagates and no observation is
    returned (fail closed). With an account, verifiers run as that dedicated user
    (verifier_account.py), in its run root and with its Python, one at a time.
    """
    if account is not None:
        run_root, python = account.run_root, account.python
        with account.exclusive():
            if not account.available():
                raise VerifierAccountError(f"account {account.user} is not usable")
            account.require_idle()
            return _run_all(
                repo,
                candidate_commit,
                specs,
                run_root,
                run_id,
                python,
                timeout,
                attempt_id,
                account,
            )
    return _run_all(
        repo, candidate_commit, specs, run_root, run_id, python, timeout, attempt_id, None
    )


def _run_all(
    repo, candidate_commit, specs, run_root, run_id, python, timeout, attempt_id, account
) -> tuple[VerifierRun, ...]:
    if not os.path.isabs(python):
        raise VerifierRejected(f"python must be an absolute path, got {python!r}")
    ids = [spec.verifier_id for spec in specs]
    if len(ids) != len(set(ids)):
        raise VerifierRejected(f"duplicate verifier ids in {ids}")
    runs = []
    for index, spec in enumerate(specs):
        clone_id = f"{run_id}-v{index}"
        checkout = create_attempt_clone(repo, candidate_commit, run_root, clone_id).path
        try:
            runs.append(
                _run_one(
                    repo, candidate_commit, spec, checkout, python, timeout, attempt_id, account
                )
            )
        finally:
            try:
                remove_attempt_clone(run_root, clone_id)
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


def _run_one(
    repo, candidate_commit, spec, checkout, python, timeout, attempt_id, account
) -> VerifierRun:
    private = Path(tempfile.mkdtemp(prefix=f"{checkout.name}.", dir=checkout.parent))
    log_path, ini, profile_path = (
        private / "verifier.log",
        private / "pytest.ini",
        private / "profile.sb",
    )
    run_dir, home, tmp, report, env = _prepare_run(private, ini)
    if account is not None:
        _share_with_account(private, ini, run_dir, home, tmp)
    if attempt_id:
        env[ATTEMPT_ENV] = attempt_id
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
    profile = None
    if sandbox.available():
        profile = sandbox.build_profile(
            read_roots=(*_interpreter_roots(python), ini),
            write_roots=(checkout, run_dir),
        )
        _write_new(profile_path, profile.text.encode())
        returncode, timed_out, survivors = _execute(
            argv, profile_path, checkout, env, timeout, log_path, account
        )
    else:
        _write_new(log_path, b"sandbox-exec unavailable: verifier not executed\n")
        returncode, timed_out, survivors = None, False, 0
    mutated = tuple(tree_differences(repo, candidate_commit, checkout))
    ran = profile is not None and _report_shows_passing_tests(report)
    too_big = log_path.stat().st_size > MAX_LOG_BYTES
    passed = (
        profile is not None
        and returncode == 0
        and not timed_out
        and not mutated
        and ran
        and not too_big
        and survivors == 0
    )
    for scratch in (home, tmp):
        if account is not None:
            account.remove(scratch)
        shutil.rmtree(scratch, ignore_errors=True)
    digest = _record_digest(
        candidate_commit=candidate_commit,
        verifier_id=spec.verifier_id,
        argv=argv,
        sandbox_profile_sha256=profile.sha256 if profile else None,
        returncode=returncode,
        timed_out=timed_out,
        mutated=mutated,
        tests_passed=ran,
        survivors=survivors,
        log_sha256=_file_sha256(log_path),
    )
    observation = VerifierObservation(
        spec.verifier_id, f"{private.name}/verifier.log", f"sha256:{digest}", passed
    )
    return VerifierRun(spec, returncode, timed_out, mutated, log_path, observation)


def _execute(argv, profile_path, checkout, env, timeout, log_path, account):
    """Run argv under the sandbox profile, as the verifier account when one is given."""
    if account is None:
        return confined.execute(sandbox.wrap(argv, profile_path), checkout, env, timeout, log_path)
    os.chmod(profile_path, 0o640)
    returncode, timed_out, survivors = confined.execute(
        account.wrap(argv, profile_path, env),
        checkout,
        env,
        timeout,
        log_path,
        reap=account.reap,
    )
    return returncode, timed_out, survivors


def _share_with_account(private: Path, ini: Path, run_dir: Path, home: Path, tmp: Path) -> None:
    """Let the verifier account (group cairnwork) enter the run dir and read the ini,
    and write only its run/ scratch. The log stays owner-only."""
    os.chmod(private, 0o750)
    os.chmod(ini, 0o640)
    for directory in (run_dir, home, tmp):
        os.chmod(directory, 0o2770)


def _prepare_run(private: Path, ini: Path):
    """Create the per-run directory (the only place besides the checkout the child may
    write), its HOME and TMPDIR, and the scrubbed environment."""
    run_dir = private / "run"
    home, tmp = run_dir / "home", run_dir / "tmp"
    for directory in (run_dir, home, tmp):
        directory.mkdir()
    _write_new(ini, b"[pytest]\n")
    env = {key: os.environ[key] for key in _ENV_KEEP if key in os.environ}
    env.update(
        HOME=str(home),
        TMPDIR=str(tmp),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONNOUSERSITE="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
    )
    env[RUN_TOKEN_VAR] = secrets.token_hex(16)
    return run_dir, home, tmp, run_dir / "junit.xml", env


def _record_digest(**fields) -> str:
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


@lru_cache(maxsize=8)
def _interpreter_roots(python: str) -> tuple[Path, ...]:
    """Directories the interpreter needs to read: its prefixes and its real location."""
    done = subprocess.run(  # noqa: S603 - trusted interpreter path chosen by Cairnwork
        [
            python,
            "-I",
            "-c",
            "import json, sys; print(json.dumps([sys.prefix, sys.base_prefix,"
            " sys.exec_prefix, sys.base_exec_prefix]))",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    roots = {Path(p) for p in json.loads(done.stdout)}
    roots.add(Path(os.path.realpath(shutil.which(python) or python)).parent)
    return tuple(sorted(roots))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _report_shows_passing_tests(report: Path) -> bool:
    """The JUnit report must exist and show at least one test run, none failed or skipped all."""
    try:
        fd = os.open(report, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return False
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
