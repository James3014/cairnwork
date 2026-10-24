"""Can the verifier account run its Python? A precheck with a reason (#234).

The stress replay lost 26 rows to a bare "not usable": the venv sat under a 0750 home. ``walk``
says for each directory on the way to the interpreter (as configured and as really located)
whether the account's uid and groups may traverse it; ``blocked`` names the first that blocks,
e.g. ``/Users/james 0750 james:staff not traversable by cairnwork-verifier``. Mode bits only, no
ACLs: a hint. ``precheck`` takes its verdict from ``VerifierAccount.available()``, before anything
is spent; a failure is UNCOMPARABLE ``VERIFIER_ACCOUNT_UNUSABLE``, never success.
"""

from __future__ import annotations

import grp
import os
import pwd
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from cairnwork.deferred_tests import BaseUncomparable, Comparison
from cairnwork.verifier_account import VerifierAccount, VerifierAccountError

UNUSABLE = "VERIFIER_ACCOUNT_UNUSABLE"


def _ids(user: str) -> tuple[int, frozenset[int]] | None:
    try:
        entry = pwd.getpwnam(user)
    except KeyError:
        return None
    return entry.pw_uid, frozenset(os.getgrouplist(user, entry.pw_gid))


def _name(lookup: Any, key: int) -> str:
    try:
        return lookup(key)[0]
    except KeyError:
        return str(key)


def _paths(python: str) -> list[str]:
    """Each directory from / down to the interpreter, as configured and as really located, then
    the interpreter itself (last, once per spelling)."""
    out: list[str] = []
    for leaf in dict.fromkeys((os.path.abspath(python), os.path.realpath(python))):
        parts = leaf.split(os.sep)[1:]
        out += [os.sep + os.sep.join(parts[:i]) for i in range(len(parts))] + [leaf]
    return list(dict.fromkeys(out))


def walk(python: str, user: str) -> list[dict[str, Any]]:
    """For each path ``_paths`` names: its mode, owner and whether ``user`` may traverse it
    (directories) or execute it (the interpreter). POSIX picks one class: owner, else group,
    else other. Empty when the user does not exist."""
    ids = _ids(user)
    if ids is None:
        return []
    uid, gids = ids
    steps: list[dict[str, Any]] = []
    for path in _paths(python):
        try:
            info = os.stat(path)
        except OSError as exc:
            steps.append({"path": path, "ok": False, "error": type(exc).__name__})
            break
        shift = 6 if info.st_uid == uid else 3 if info.st_gid in gids else 0
        owner = f"{_name(pwd.getpwuid, info.st_uid)}:{_name(grp.getgrgid, info.st_gid)}"
        mode, ok = f"{info.st_mode & 0o7777:04o}", bool(info.st_mode >> shift & 1)
        steps.append({"path": path, "mode": mode, "owner": owner, "ok": ok})
    return steps


def blocked(python: str, user: str) -> str:
    """The first step of ``walk`` the account cannot take, in words; "" when none."""
    if _ids(user) is None:
        return f"account {user} does not exist"
    for step in walk(python, user):
        if "error" in step:
            return f"{step['path']} cannot be read ({step['error']})"
        if not step["ok"]:
            verb = "traversable" if os.path.isdir(step["path"]) else "executable"
            return f"{step['path']} {step['mode']} {step['owner']} not {verb} by {user}"
    return ""


def unusable(detail: str) -> BaseUncomparable:
    return BaseUncomparable(Comparison("UNCOMPARABLE", UNUSABLE, detail))


def precheck(
    account: VerifierAccount | None, python: str | None, timeout: float = 0.0
) -> str | None:
    """Raise UNCOMPARABLE ``VERIFIER_ACCOUNT_UNUSABLE`` when ``available()`` says the account (if
    any) cannot run ``python`` (or its own); ``blocked`` only explains. None: checked (or no
    account). It never queues behind a run (``timeout`` 0: one try): only when the lock is busy is
    nothing decided (the walk ignores ACLs, so it never refuses); the reason is returned and the
    run's own exclusive() and available() decide later, as before (#234). A lock that cannot be
    used at all (no account root, an unsafe queue) is unusable."""
    if account is None:
        return None
    py = python or account.python
    try:
        with account.exclusive(timeout):
            if account.available(py):
                return None
    except VerifierAccountError as exc:
        if "RESOURCE_LEASE_TIMEOUT" in str(exc):  # busy: deferred
            return str(exc)
        raise unusable(f"account {account.user} lock: {exc}") from exc  # e.g. an unsafe queue
    except OSError as exc:  # e.g. no account root to hold the lock file
        raise unusable(f"account {account.user} lock: {exc}") from exc
    hint = blocked(py, account.user) or "no blocked directory found; check sudo and import pytest"
    raise unusable(f"account {account.user} cannot run {py}: {hint}")


@contextmanager
def base_stage(account: VerifierAccount | None, python: str | None) -> Iterator[None]:
    """Around the base runs (deferred tests on the base, the P2P probe): no candidate code has run
    as the account yet, so an account failure there is no evidence either way. The worker path
    still reports it as UNCOMPARABLE only for an isolated worker (Comparison.worker_reason)."""
    try:
        yield
    except VerifierAccountError as exc:
        hint = blocked(python or account.python, account.user) if account is not None else ""
        raise unusable(f"{exc}; {hint}" if hint else str(exc)) from exc
