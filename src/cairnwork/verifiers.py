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
pytest (a root ``pytest.py`` cannot replace it). Callers must keep tests,
pytest configuration and shadowing root-level modules out of the files a candidate
may change (``protected_verifier_inputs``), but that is not enough on its own:
candidate source code also runs inside the pytest process and can forge a
result.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

import cairnwork_sandbox
from cairnwork import pytest_config, report_channel, report_guard
from cairnwork.confined import ATTEMPT_ENV, MAX_LOG_BYTES, RUN_TOKEN_VAR
from cairnwork.core_request import VerifierObservation
from cairnwork.reasons import why_not_passed
from cairnwork.treecompare import tree_differences
from cairnwork.verifier_account import VerifierAccount, VerifierAccountError, lock_wait_seconds
from cairnwork.verifier_exec import (  # low-level run primitives, split out for the 400-line cap
    _execute,
    _file_sha256,
    _record_digest,
    _same_directory,
    _write_new,
)
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
# Kept here (not in verifier_exec) because tests monkeypatch verifiers._ENV_KEEP to inject env vars,
# and _prepare_run below must read the patched value.
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
    report_too_big: bool = False  # the guard's report exceeded MAX_REPORT_BYTES: no verdict read
    recorded: bool = False  # the guard's record was there and well-formed (base_comparison, #234)
    collect_errors: tuple[str, ...] = ()  # collectors the guard saw fail (B2, #234)
    why: str = ""  # in words, why the run did not pass ("" when it passed): reasons.py
    # facts about the sandbox policy used (e.g. "shm withheld: BWRAP_TMPFS_SIZE_UNSUPPORTED"); not
    # in the observation's artifact hash, so recording them changes no bound digest
    notes: tuple[str, ...] = ()


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
    import_paths: tuple[str, ...] = (),
    note: str = "",
) -> tuple[VerifierRun, ...]:
    """Run each spec on a fresh checkout of candidate_commit; a failing test is not an error.

    If a checkout cannot be created the error propagates and no observation is
    returned (fail closed). With an account, verifiers run as that dedicated user
    (verifier_account.py), in its run root and with its Python (or ``python``), one at a
    time. With base_commit, pytest settings come from that commit (pytest_config.py).
    ``import_paths`` (operator state config, #82) are checkout-relative dirs prepended to the
    verifier's import path so a src-layout project can import its own package. A non-empty note
    (the P2P selection rule, p2p.py) is bound into each observation's artifact hash.
    """
    settings = pytest_config.base_settings(repo, base_commit)
    if import_paths:
        settings = replace(settings, pythonpath=(*import_paths, *settings.pythonpath))
    if account is not None:
        run_root, python = account.run_root, python or account.python
        with account.exclusive(lock_wait_seconds(len(specs), timeout)):
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
            if account is not None:  # writable as in owner mode, whatever the umask (#240)
                account.grant_write(checkout)
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
            _remove_checkout(work, clone_id, account)
    return tuple(runs)


def _remove_checkout(work: Path, clone_id: str, account) -> None:
    """Remove a verifier's work dir and checkout; a leftover is only disk space, never a verdict.

    Under the account the checkout holds files and directories the account made (#240). The
    account removes them first, while every owner directory in the checkout is still open to it;
    the owner then removes what is left (its own directories, which the account cannot chmod)
    without ever walking a tree the account wrote with chmod. remove_attempt_clone is the owner
    path only: its chmod 0o700 pass would shut the account out of its own leftovers."""
    if account is not None:
        try:
            account.remove(work)
        finally:  # whatever the account's removal did, the owner removes what it can
            shutil.rmtree(work, ignore_errors=True)
        return
    try:
        remove_attempt_clone(work, clone_id)
    except WorkspaceError:
        pass  # the run's verdict stands; a leftover directory is only disk space
    shutil.rmtree(work, ignore_errors=True)


def _remove_scratch(dirs, account) -> None:
    for scratch in dirs:
        if account is not None:
            account.remove(scratch)
        shutil.rmtree(scratch, ignore_errors=True)


def _prepare_run(private: Path, ini: Path, ini_text: bytes, channel=None):
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
    if channel is not None:  # owner run: descriptors, no nonce or path in the environment
        env.update(channel.env())
    else:  # verifier account: sudo closes descriptors, so path and nonce stay in the environment
        env[report_guard.PATH_ENV] = str(verdict_path)
        env[report_guard.NONCE_ENV] = verdict_nonce
    return run_dir, home, tmp, env, verdict_path, verdict_nonce


def _run_one(
    repo, candidate_commit, spec, checkout, python, timeout, attempt_id, account, settings, note
) -> VerifierRun:
    plugins = pytest_config.installed_plugins(python)
    ini_text = settings.ini_text(checkout, plugins)
    private = Path(tempfile.mkdtemp(prefix=f"{checkout.name}.", dir=checkout.parent.parent))
    channel = report_channel.open_channel(private) if account is None else None
    try:
        return _run_one_in(
            repo, candidate_commit, spec, checkout, python, timeout, attempt_id, account,
            settings, note, plugins, ini_text, private, channel,
        )  # fmt: skip
    finally:
        if channel is not None:
            channel.close()


def _run_one_in(
    repo, candidate_commit, spec, checkout, python, timeout, attempt_id, account, settings, note,
    plugins, ini_text, private, channel,
) -> VerifierRun:  # fmt: skip
    log_path, ini, profile_path = (
        private / "verifier.log",
        private / "pytest.ini",
        private / "profile.sb",
    )
    run_dir, home, tmp, env, verdict_path, verdict_nonce = _prepare_run(
        private, ini, ini_text, channel
    )
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
        fds = channel.pass_fds if channel else ()
        returncode, timed_out, survivors = _execute(
            argv, profile_path, checkout, env, timeout, log_path, account, backend, fds
        )
    else:
        _write_new(log_path, b"no sandbox backend available: verifier not executed\n")
        returncode, timed_out, survivors = None, False, 0
    if _same_directory(checkout, before):  # not renamed away, no symlink put in place
        mutated = tuple(tree_differences(repo, candidate_commit, checkout))
    else:
        mutated = ("checkout replaced",)
    verdict = channel.read() if channel else report_guard.read_verdict(verdict_path, verdict_nonce)
    ran = profile is not None and verdict.ran
    clean = not timed_out and not mutated and survivors == 0
    too_big = log_path.stat().st_size > MAX_LOG_BYTES
    clean = clean and not too_big and not verdict.too_big
    passed = profile is not None and returncode == 0 and clean and ran
    plain_failure = (
        profile is not None and returncode == 1 and clean and "failed" in verdict.outcomes.values()
    )
    why = "" if passed else why_not_passed(
        ran_sandboxed=profile is not None, returncode=returncode, timed_out=timed_out,
        mutated=mutated, survivors=survivors, log_too_big=too_big, tests_ran=ran,
        report_too_big=verdict.too_big,
    )  # fmt: skip
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
        survivors=survivors, ran=ran, plain_failure=plain_failure, why=why,
        report_too_big=verdict.too_big, notes=profile.notes if profile else (),
        recorded=verdict.recorded, collect_errors=verdict.collect_errors,
    )  # fmt: skip
