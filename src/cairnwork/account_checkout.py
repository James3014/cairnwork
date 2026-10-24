"""A verifier checkout the verifier account owns (Issue #240), and checkout removal.

In owner mode the sandboxed verifier runs as the owner of its checkout: it may write files, set
explicit times (``os.utime(path, (t, t))``), chmod and atomically replace them. Write permission
alone gives another uid only the first: explicit times and chmod need ownership (POSIX), so an
honest test such as a template cache keyed by mtime failed under the account with EACCES.

So under the account the checkout is the account's own copy:

1. the owner clones the candidate as always (``create_attempt_clone``: ``--no-local``, base
   history only, checked against the tree) into ``work/.cairnwork-source/<clone_id>`` and opens it
   to the account for reading only (``VerifierAccount.grant_read``, #91);
2. AS THE ACCOUNT, a fixed helper (``COPY_HELPER``, run by the verifier's own Python with ``-I -S``
   through the same sudo + sandbox-binary path ``account.remove`` uses) copies it to
   ``work/<clone_id>``: directories recreated (iteratively: any depth), regular files copied with
   mode and times (every byte written), symlinks copied as symlinks (never followed), any other
   file type refused before it is opened, and the destination must not exist;
3. the owner checks the copy (a real directory, by lstat, owned by the account, equal to the
   candidate tree) and removes the staging clone. The verifier then runs in ``work/<clone_id>``.

After the run (the account's processes are killed by then) the owner compares the copy
(``checkout_differences``): ``_same_directory`` first, then, AS THE ACCOUNT, ``SHARE_HELPER`` copies
each directory's and regular file's user r and x bits to group and other, and then the owner runs
``tree_differences``. So the owner reads exactly what it could read as the checkout's owner in
owner mode: a file replaced from a 0600 temporary file or a directory left at 0700 is compared as
in owner mode (mode bits other than the owner's execute bit are not judged), and a path closed to
its own owner (0000) stays "unreadable", which fails the run in both modes.

A helper that outlives its timeout (``subprocess.run`` kills only sudo) is ended with
``account.kill_all()``: callers hold the account's lock (``VerifierAccount.exclusive``), so every
process of the account is this run's. ``remove_checkout`` removes the work dir: the account first,
then the owner; a failure there is reported (ALERT) and never changes the run's verdict. Nothing
in the checkout is ever executed, used as git state or trusted by the owner.
"""

from __future__ import annotations

import os
import pwd
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from cairnwork.treecompare import tree_differences
from cairnwork.verifier_account import VerifierAccountError
from cairnwork.verifier_exec import _same_directory
from cairnwork.workspace import WorkspaceError, create_attempt_clone, remove_attempt_clone

STAGE = ".cairnwork-source"
COPY_TIMEOUT_SECONDS = 600
SHARE_TIMEOUT_SECONDS = 120

# Runs as the account with ``python -I -S -c COPY_HELPER <source> <target>``. Exit 0 only if
# every entry was copied; any error (a refused file type included) exits non-zero.
COPY_HELPER = r"""
import os, stat, sys

def copy_file(s, d, info):
    fd = os.open(s, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            sys.exit(f"swapped while copying: {s}")
        out = os.open(d, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            while True:
                chunk = memoryview(os.read(fd, 1 << 20))
                if not chunk:
                    break
                while chunk:  # os.write may write less than it was given
                    chunk = chunk[os.write(out, chunk):]
            os.fchmod(out, stat.S_IMODE(info.st_mode))
            os.utime(out, ns=(info.st_atime_ns, info.st_mtime_ns))
        finally:
            os.close(out)
    finally:
        os.close(fd)

def copy(source, target):
    stack = [(source, target, None)]  # an explicit stack, not recursion: any depth
    while stack:
        src, dst, done = stack.pop()
        if done is not None:  # after its contents: the directory's own mode and times
            os.chmod(dst, stat.S_IMODE(done.st_mode))
            os.utime(dst, ns=(done.st_atime_ns, done.st_mtime_ns))
            continue
        os.mkdir(dst, 0o700)
        stack.append((src, dst, os.lstat(src)))
        for name in sorted(os.listdir(src)):
            s, d = os.path.join(src, name), os.path.join(dst, name)
            info = os.lstat(s)
            if stat.S_ISLNK(info.st_mode):
                os.symlink(os.readlink(s), d)
            elif stat.S_ISDIR(info.st_mode):
                stack.append((s, d, None))
            elif stat.S_ISREG(info.st_mode):
                copy_file(s, d, info)
            else:
                sys.exit(f"refused file type: {s}")

source, target = sys.argv[1], sys.argv[2]
if os.path.lexists(target):
    sys.exit(f"target exists: {target}")
if not stat.S_ISDIR(os.lstat(source).st_mode):
    sys.exit(f"source is not a directory: {source}")
copy(source, target)
"""

# Runs as the account with ``python -I -S -c SHARE_HELPER <checkout>`` after the run: symlinks
# and other file types are skipped (lchmod where the platform has it), the top-level .git (not
# compared) is skipped, and any error leaves that path as it was: the owner then finds it
# "unreadable", which fails closed.
SHARE_HELPER = r"""
import os, stat, sys

NOFOLLOW = {"follow_symlinks": False} if os.chmod in os.supports_follow_symlinks else {}
stack = [(sys.argv[1], True)]  # an explicit stack, not recursion: any depth
while stack:
    path, top = stack.pop()
    try:
        info = os.lstat(path)
    except OSError:
        continue
    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        continue
    user = info.st_mode & 0o500
    try:
        os.chmod(path, stat.S_IMODE(info.st_mode) | user >> 3 | user >> 6, **NOFOLLOW)
    except OSError:
        pass
    if stat.S_ISDIR(info.st_mode):
        try:
            names = os.listdir(path)
        except OSError:
            continue
        stack += [(os.path.join(path, n), False) for n in names if not (top and n == ".git")]
"""


def _as_account(account, python: str, helper: str, args: list[str], timeout: int):
    """Run a fixed helper as the account, unconfined; a timeout also ends the helper itself."""
    argv = account.wrap([python, "-I", "-S", "-c", helper, *args], None)
    try:
        return subprocess.run(  # noqa: S603 - fixed helper, paths made here
            argv, capture_output=True, text=True, timeout=timeout, check=False, cwd=account.root
        )
    except subprocess.TimeoutExpired:
        account.kill_all()  # the caller holds the account's lock: its processes are this run's
        raise


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
        done = _as_account(
            account, python, COPY_HELPER, [str(source), str(target)], COPY_TIMEOUT_SECONDS
        )
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


def checkout_differences(repo: Path, commit: str, checkout: Path, before, account, python: str):
    """What the run changed in its checkout, as ``tree_differences`` names it; under the account,
    first open the account's copy to the owner as described above."""
    if not _same_directory(checkout, before):  # renamed away, or a symlink put in place
        return ("checkout replaced",)
    if account is not None:
        try:
            _as_account(account, python, SHARE_HELPER, [str(checkout)], SHARE_TIMEOUT_SECONDS)
        except (OSError, subprocess.SubprocessError):
            pass  # what stays closed is "unreadable" below: the run fails, never passes
    return tuple(tree_differences(repo, commit, checkout))


def remove_checkout(work: Path, clone_id: str, account) -> None:
    """Remove a verifier's work dir and checkout. A leftover is only disk space and never changes
    the run's verdict (decided before this runs), but it is reported (ALERT on stderr).

    Under the account the checkout is the account's own copy: only the account can remove it
    (``account.remove``), and that is what guarantees it goes. Its failure (a path outside its run
    root, ``rm`` timing out on a huge tree) is caught; a timed-out ``rm`` is ended by the next
    ``kill_all`` (the run's reap or the next ``require_idle``). The owner then removes the rest of
    ``work`` (it owns ``work`` and any staging clone a failure left). That rmtree may walk what the
    account left, but it never follows a symlink out (``shutil.rmtree.avoids_symlink_attacks``,
    true on macOS and Linux, pinned by a test) and only unlinks names. remove_attempt_clone is the
    owner path only: its chmod pass cannot touch the account's directories."""
    try:
        if account is not None:
            account.remove(work)
        else:
            remove_attempt_clone(work, clone_id)
    except (WorkspaceError, VerifierAccountError, OSError, subprocess.SubprocessError):
        pass  # the run's verdict stands; what is left is reported below
    finally:  # whatever the first removal did, the owner removes what it can
        shutil.rmtree(work, ignore_errors=True)
        if os.path.lexists(work):
            print(f"ALERT verifier cleanup: {work} was not removed", file=sys.stderr, flush=True)
