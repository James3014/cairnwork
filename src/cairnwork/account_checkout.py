"""A verifier checkout the verifier account owns (Issue #240).

In owner mode the sandboxed verifier runs as the owner of its checkout: it may write files, set
explicit times (``os.utime(path, (t, t))``), chmod and atomically replace them. Write permission
alone gives another uid only the first: explicit times and chmod need ownership (POSIX), so an
honest test such as a template cache keyed by mtime failed under the account with EACCES.

So under the account the checkout is the account's own copy:

1. the owner clones the candidate as always (``create_attempt_clone``: ``--no-local``, base
   history only, checked against the tree) into ``work/.cairnwork-source/<clone_id>`` and opens it
   to the account for reading only (``VerifierAccount.grant_read``, #91);
2. AS THE ACCOUNT, a fixed helper (``COPY_HELPER``, run by the verifier's own Python with ``-I``
   through the same sudo + sandbox-binary path ``account.remove`` uses) copies it to
   ``work/<clone_id>``: directories recreated, regular files copied with mode and times, symlinks
   copied as symlinks (never followed), any other file type refused before it is opened, and the
   destination must not exist;
3. the owner checks the copy (a real directory owned by the account, equal to the candidate tree)
   and removes the staging clone. The verifier then runs in ``work/<clone_id>`` as before.

What the owner does with the copy afterwards is unchanged: ``_same_directory`` and
``tree_differences`` (it reads files through the copied group/other read bits; a path the test
closes is "unreadable", which fails the run, as in owner mode), and removal by the account first,
then the owner (``verifiers._remove_checkout``). Nothing in the checkout is ever executed, used as
git state or trusted by the owner.
"""

from __future__ import annotations

import os
import pwd
import shutil
import stat
import subprocess
from pathlib import Path

from cairnwork.treecompare import tree_differences
from cairnwork.workspace import WorkspaceError, create_attempt_clone, remove_attempt_clone

STAGE = ".cairnwork-source"
COPY_TIMEOUT_SECONDS = 600

# Runs as the account with ``python -I -c COPY_HELPER <source> <target>``. Exit 0 only if every
# entry was copied; any error (a refused file type included) exits non-zero.
COPY_HELPER = r"""
import os, stat, sys

def copy(src, dst):
    os.mkdir(dst, 0o700)
    for name in sorted(os.listdir(src)):
        s, d = os.path.join(src, name), os.path.join(dst, name)
        info = os.lstat(s)
        if stat.S_ISLNK(info.st_mode):
            os.symlink(os.readlink(s), d)
        elif stat.S_ISDIR(info.st_mode):
            copy(s, d)
        elif stat.S_ISREG(info.st_mode):
            fd = os.open(s, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    sys.exit(f"swapped while copying: {s}")
                out = os.open(d, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                try:
                    while True:
                        chunk = os.read(fd, 1 << 20)
                        if not chunk:
                            break
                        os.write(out, chunk)
                    os.fchmod(out, stat.S_IMODE(info.st_mode))
                    os.utime(out, ns=(info.st_atime_ns, info.st_mtime_ns))
                finally:
                    os.close(out)
            finally:
                os.close(fd)
        else:
            sys.exit(f"refused file type: {s}")
    info = os.lstat(src)
    os.chmod(dst, stat.S_IMODE(info.st_mode))
    os.utime(dst, ns=(info.st_atime_ns, info.st_mtime_ns))

source, target = sys.argv[1], sys.argv[2]
if os.path.lexists(target):
    sys.exit(f"target exists: {target}")
if not stat.S_ISDIR(os.lstat(source).st_mode):
    sys.exit(f"source is not a directory: {source}")
copy(source, target)
"""


def make_account_checkout(account, python: str, repo: Path, commit: str, work: Path, clone_id: str):
    """Return ``work/<clone_id>``, a checkout of ``commit`` owned by the account; raise
    WorkspaceError if it cannot be made exactly. ``python`` is the verifier's interpreter, already
    proven runnable as the account (``available``). The staging clone is always removed."""
    stage = work / STAGE
    stage.mkdir(mode=0o750)
    target = work / clone_id
    try:
        source = create_attempt_clone(repo, commit, stage, clone_id).path
        account.grant_read(stage)
        argv = account.wrap([python, "-I", "-c", COPY_HELPER, str(source), str(target)], None)
        done = subprocess.run(  # noqa: S603 - fixed helper, paths made here
            argv, capture_output=True, text=True, timeout=COPY_TIMEOUT_SECONDS, check=False,
            cwd=account.root,
        )  # fmt: skip
        if done.returncode != 0:
            raise WorkspaceError(f"account copy failed: {done.stderr.strip()[-500:]}")
        _check_copy(target, account, repo, commit)
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkspaceError(f"account copy failed: {exc}") from exc
    finally:
        try:
            remove_attempt_clone(stage, clone_id)
        except WorkspaceError:
            pass  # a leftover staging clone is only disk space; rmtree below tries again
        shutil.rmtree(stage, ignore_errors=True)
    return target


def _check_copy(target: Path, account, repo: Path, commit: str) -> None:
    info = os.lstat(target)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != pwd.getpwnam(account.user).pw_uid:
        raise WorkspaceError(f"{target} is not a directory owned by {account.user}")
    differences = tree_differences(repo, commit, target)
    if differences:
        raise WorkspaceError(f"account copy differs from {commit}: {', '.join(differences[:5])}")
