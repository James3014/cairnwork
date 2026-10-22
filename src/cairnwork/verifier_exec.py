"""Low-level run primitives for a single verifier execution (split out of verifiers.py for the
400-line cap).

The sandboxed subprocess launch, the per-run directory/environment setup, and the small file/digest
helpers. Behaviour is unchanged — this is a move, not a logic change. ``verifiers.py`` orchestrates
these (``_run_one``); they never call back into it, so there is no import cycle.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from pathlib import Path

from cairnwork import confined, report_guard
from cairnwork.confined import RUN_TOKEN_VAR

_ENV_KEEP = ("PATH", "LANG", "LC_ALL")


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
