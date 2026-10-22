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
xfailed. pytest's configuration is pinned to a Cairnwork-owned ini with rootdir
and confcutdir at the checkout, so no ini in the candidate or a parent directory
applies, and plugin autoloading is off; the ini holds only allow-listed keys from
the task's base commit and plugins load by explicit -p (pytest_config.py). Output
streams to a log file with a size cap, the run's process group is killed afterwards, and logs and
reports live in a private directory, created with O_EXCL|O_NOFOLLOW. A process
that starts its own session escapes the group kill; only a sandbox stops that.
The observation hash binds the candidate commit, verifier id, full argv, exit
code, timeout, mutations, test result and log hash.

The run is confined with macOS Seatbelt (see cairnwork_sandbox): no network, reads only
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
result.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import cairnwork_sandbox
from cairnwork import confined, pytest_config, report_guard
from cairnwork.confined import ATTEMPT_ENV, MAX_LOG_BYTES, RUN_TOKEN_VAR
from cairnwork.core_request import VerifierObservation
from cairnwork.treecompare import tree_differences
from cairnwork.verifier_account import VerifierAccount, VerifierAccountError
from cairnwork.verifier_spec import (  # re-exported for existing importers
    VerifierRejected,
    VerifierSpec,
    parse_verifier,
    protected_verifier_inputs,
)
from cairnwork.workspace import WorkspaceError, create_attempt_clone, remove_attempt_clone

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "VerifierRejected",
    "VerifierRun",
    "VerifierSpec",
    "parse_verifier",
    "protected_verifier_inputs",
    "run_verifiers",
]

DEFAULT_TIMEOUT_SECONDS = 600
_ENV_KEEP = ("PATH", "LANG", "LC_ALL")


@dataclass(frozen=True)
class VerifierRun:
    spec: VerifierSpec
    returncode: int | None
    timed_out: bool
    mutated: tuple[str, ...]
    log_path: Path
    observation: VerifierObservation
    outcomes: dict[str, str]
    survivors: int = 0
    ran: bool = False
    # a plain test failure: the guard reported a failed test and nothing about the run itself
    # is suspect (no timeout, mutation, survivor or oversized log); p2p.py may re-run only this
    plain_failure: bool = False


def run_verifiers(
    repo: Path,
    candidate_commit: str,
    specs: tuple[VerifierSpec, ...],
    run_root: Path,
    run_id: str,
    *,
    python: str | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    attempt_id: str = "",
    account: VerifierAccount | None = None,
    base_commit: str | None = None,
    note: str = "",
) -> tuple[VerifierRun, ...]:
    """Run each spec on a fresh checkout of candidate_commit; a failing test is not an error.

    If a checkout cannot be created the error propagates and no observation is
    returned (fail closed). With an account, verifiers run as that dedicated user
    (verifier_account.py), in its run root and with its Python (or ``python``), one at a
    time. With base_commit, pytest settings come from that commit (pytest_config.py). A non-empty
    note (the P2P selection rule, p2p.py) is bound into each observation's artifact hash.
    """
    settings = pytest_config.base_settings(repo, base_commit)
    if account is not None:
        run_root, python = account.run_root, python or account.python
        with account.exclusive():
            if not account.available(python):
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
                settings,
                note,
            )
    python = python or sys.executable
    return _run_all(
        repo,
        candidate_commit,
        specs,
        run_root,
        run_id,
        python,
        timeout,
        attempt_id,
        None,
        settings,
        note,
    )


def _run_all(
    repo,
    candidate_commit,
    specs,
    run_root,
    run_id,
    python,
    timeout,
    attempt_id,
    account,
    settings,
    note,
) -> tuple[VerifierRun, ...]:
    if not os.path.isabs(python):
        raise VerifierRejected(f"python must be an absolute path, got {python!r}")
    ids = [spec.verifier_id for spec in specs]
    if len(ids) != len(set(ids)):
        raise VerifierRejected(f"duplicate verifier ids in {ids}")
    runs = []
    for index, spec in enumerate(specs):
        clone_id = f"{run_id}-v{index}"
        # The checkout sits alone in its own work directory, which the tests may write:
        # some create files next to their repository (Nexus-new: nexus-runtime-targets),
        # and that must stay inside this run. Sticky: the account cannot move the checkout.
        work = Path(run_root) / f"{clone_id}.work"
        work.parent.mkdir(parents=True, exist_ok=True)
        work.mkdir(mode=0o700)
        try:
            if account is not None:
                os.chmod(work, 0o3770)
            checkout = create_attempt_clone(repo, candidate_commit, work, clone_id).path
            if account is not None:
                account.grant_read(checkout)  # whatever the caller's umask was
            runs.append(
                _run_one(
                    repo,
                    candidate_commit,
                    spec,
                    checkout,
                    python,
                    timeout,
                    attempt_id,
                    account,
                    settings,
                    note,
                )
            )
        finally:
            try:
                remove_attempt_clone(work, clone_id)
            except WorkspaceError:
                pass  # the run's verdict stands; a leftover directory is only disk space
            _remove_scratch((work,), account)
    return tuple(runs)


def _remove_scratch(dirs, account) -> None:
    for scratch in dirs:
        if account is not None:
            account.remove(scratch)
        shutil.rmtree(scratch, ignore_errors=True)


def _run_one(
    repo, candidate_commit, spec, checkout, python, timeout, attempt_id, account, settings, note
) -> VerifierRun:
    plugins = pytest_config.installed_plugins(python)
    ini_text = settings.ini_text(checkout, plugins)
    private = Path(tempfile.mkdtemp(prefix=f"{checkout.name}.", dir=checkout.parent.parent))
    log_path, ini, profile_path = (
        private / "verifier.log",
        private / "pytest.ini",
        private / "profile.sb",
    )
    run_dir, home, tmp, env, verdict_path, verdict_nonce = _prepare_run(private, ini, ini_text)
    if account is not None:
        account.share_run(private, ini, run_dir, home, tmp)
    if attempt_id:
        env[ATTEMPT_ENV] = attempt_id
    argv = [
        python, "-P", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-c", str(ini),
        f"--rootdir={checkout}", f"--confcutdir={checkout}", "-o", "addopts=",
        *settings.args(plugins), "-p", report_guard.MODULE, *spec.targets,
    ]  # fmt: skip
    before = os.lstat(checkout)
    profile = None
    backend = cairnwork_sandbox.select()
    if backend is not None:
        profile = backend.policy(
            read_roots=(*pytest_config.interpreter_roots(python), ini, private / "guard"),
            write_roots=(checkout.parent, run_dir),
            mask_home_credentials=account is None,  # account uid can't enter the owner's home
        )
        _write_new(profile_path, profile.text.encode())
        returncode, timed_out, survivors = _execute(
            argv, profile_path, checkout, env, timeout, log_path, account, backend
        )
    else:
        _write_new(log_path, b"no sandbox backend available: verifier not executed\n")
        returncode, timed_out, survivors = None, False, 0
    if _same_directory(checkout, before):  # not renamed away, no symlink put in place
        mutated = tuple(tree_differences(repo, candidate_commit, checkout))
    else:
        mutated = ("checkout replaced",)
    verdict = report_guard.read_verdict(verdict_path, verdict_nonce)
    ran = profile is not None and verdict.ran
    clean = not timed_out and not mutated and survivors == 0
    clean = clean and log_path.stat().st_size <= MAX_LOG_BYTES
    passed = profile is not None and returncode == 0 and clean and ran
    plain_failure = (
        profile is not None and returncode == 1 and clean and "failed" in verdict.outcomes.values()
    )
    _remove_scratch((home, tmp), account)
    digest = _record_digest(
        candidate_commit=candidate_commit,
        verifier_id=spec.verifier_id,
        argv=argv,
        pytest_ini_sha256=hashlib.sha256(ini_text).hexdigest(),
        sandbox_profile_sha256=profile.sha256 if profile else None,
        returncode=returncode,
        timed_out=timed_out,
        mutated=mutated,
        tests_passed=ran,
        survivors=survivors,
        log_sha256=_file_sha256(log_path),
        **({"note": note} if note else {}),
    )
    observation = VerifierObservation(
        spec.verifier_id, f"{private.name}/verifier.log", f"sha256:{digest}", passed
    )
    return VerifierRun(
        spec, returncode, timed_out, mutated, log_path, observation, verdict.outcomes,
        survivors=survivors, ran=ran, plain_failure=plain_failure,
    )  # fmt: skip


def _same_directory(path: Path, before: os.stat_result) -> bool:
    try:
        after = os.lstat(path)
    except OSError:
        return False
    same = (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    return same and stat.S_ISDIR(after.st_mode)


def _execute(argv, profile_path, checkout, env, timeout, log_path, account, backend):
    """Run argv under the sandbox profile, as the verifier account when one is given.
    As if the verifier's venv were activated, its bin directory (argv[0] is its Python)
    leads PATH: tests that spawn plain ``python`` get the same interpreter as pytest."""
    env["PATH"] = os.pathsep.join(filter(None, (str(Path(argv[0]).parent), env.get("PATH"))))
    # The verifier account does not own the checkout, so git would refuse it as a
    # "dubious ownership" repository; trust exactly this checkout, nothing wider.
    env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="safe.directory")
    env["GIT_CONFIG_VALUE_0"] = os.path.realpath(checkout)
    if account is None:
        return confined.execute(backend.wrap(argv, profile_path), checkout, env, timeout, log_path)
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


def _prepare_run(private: Path, ini: Path, ini_text: bytes):
    """Create the per-run directory (the only place besides the checkout the child may
    write), its HOME and TMPDIR, and the scrubbed environment."""
    run_dir = private / "run"
    home, tmp = run_dir / "home", run_dir / "tmp"
    for directory in (run_dir, home, tmp):
        directory.mkdir()
    guard = private / "guard"
    guard.mkdir()
    report_guard.install_plugin(guard)  # the trusted plugin, read-only to the sandbox
    guard.chmod(0o750)
    _write_new(ini, ini_text)
    env = {key: os.environ[key] for key in _ENV_KEEP if key in os.environ}
    env.update(
        HOME=str(home),
        TMPDIR=str(tmp),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONNOUSERSITE="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTHONPATH=str(guard),
    )
    env[RUN_TOKEN_VAR] = secrets.token_hex(16)
    verdict_path, verdict_nonce = run_dir / "verdict.json", secrets.token_hex(16)
    env[report_guard.PATH_ENV] = str(verdict_path)
    env[report_guard.NONCE_ENV] = verdict_nonce
    return run_dir, home, tmp, env, verdict_path, verdict_nonce


def _record_digest(**fields) -> str:
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_new(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
