"""Open a fresh verifier checkout to the verifier account for reading AND writing (Issue #240).

In owner mode the sandboxed verifier runs as the owner and may write its checkout (the policy's
write roots are the checkout's ``work`` dir and the run scratch in both modes). Under the verifier
account only the Unix permissions differ, so an honest test that writes inside its checkout (a
template cache, a fixture it then deletes) failed with EACCES there and passed in owner mode.
``grant_write`` closes that gap: what a test may do to the checkout no longer depends on the mode.

The bits go to group AND other. The group is ``cairnwork`` on macOS (a new file takes its
directory's group) and on Linux when the owner is an effective member; it is the owner's primary
group when the per-run chmod stripped setgid from ``work`` (Issue #91). Either way only the owner
and the account can reach the checkout: ``work`` is 0o3770, group ``cairnwork``, nothing for other.

Safety of the walk itself:

- it never follows a symlink: each entry is opened with ``O_NOFOLLOW`` relative to its directory's
  descriptor and changed with ``fchmod`` on that descriptor, so a name swapped for a symlink is
  refused (ELOOP), never resolved;
- it is bottom-up: a directory becomes writable to the account only after everything inside it is
  done, so even a racing account process could not swap an entry the walk has yet to reach;
- a regular file with another hard link keeps read-only access: widening that inode would widen
  the other name too, which may live outside the checkout. A ``--no-local`` clone has none;
- anything that is not a directory or a regular file (a FIFO, a device) is left unchanged.

Files the account then creates are its own; ``VerifierAccount.remove`` deletes them, and every
owner directory in the checkout stays open to it until the account's cleanup has run
(verifiers._remove_checkout, recovery._remove_account_leftovers).
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

DIR_BITS = 0o077  # group and other: list, enter, create, delete
FILE_BITS = 0o066  # group and other: read and write (execute bits are left as tracked)
READ_BITS = 0o044  # a hard-linked file: read only, as before #240


def grant_write(root: Path) -> None:
    """Let the account read and write every directory and regular file of the checkout at
    ``root``, whatever the umask was. Fails (OSError) on anything it cannot open, rather than
    leaving part of the tree closed without saying so."""
    top = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _grant_tree(top)
    finally:
        os.close(top)


def _grant_tree(dir_fd: int) -> None:
    """Everything inside the directory open at ``dir_fd`` first, then the directory itself.
    Not os.fwalk: that opens (follows) a symlink to a directory before it skips it."""
    for name in os.listdir(dir_fd):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:  # a symlink (tracked or swapped in): never followed
                continue
            raise
        try:
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                _grant_tree(fd)
            else:
                _widen(fd)
        finally:
            os.close(fd)
    _widen(dir_fd)


def _widen(fd: int) -> None:
    info = os.fstat(fd)
    if stat.S_ISDIR(info.st_mode):
        bits = DIR_BITS
    elif stat.S_ISREG(info.st_mode):
        bits = FILE_BITS if info.st_nlink == 1 else READ_BITS
    else:
        return
    os.fchmod(fd, stat.S_IMODE(info.st_mode) | bits)
