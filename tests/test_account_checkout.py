"""#240: the account-owned checkout (account_checkout.py), without sudo.

The copy helper runs here as the owner: symlinks stay symlinks, regular files keep bytes, mode
and times, other file types are refused before they are opened, an existing target is refused.
``make_account_checkout`` runs with a stand-in account whose "sudo" is a plain subprocess: the copy
must be a directory owned by the account and equal to the candidate tree, and the staging clone is
removed whatever happens. The real account path is in test_checkout_write_parity.py.
"""

import os
import pwd
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import git

from cairnwork.account_checkout import COPY_HELPER, STAGE, make_account_checkout
from cairnwork.verifier_account import VerifierAccount
from cairnwork.workspace import WorkspaceError


def _copy(source, target, timeout=20):
    return subprocess.run(  # noqa: S603 - the helper under test
        [sys.executable, "-I", "-c", COPY_HELPER, str(source), str(target)],
        capture_output=True, text=True, timeout=timeout, check=False,
    )  # fmt: skip


def test_copy_keeps_bytes_modes_times_and_symlinks(tmp_path):
    source, outside = tmp_path / "src", tmp_path / "outside"
    (source / "d").mkdir(parents=True)
    outside.mkdir()
    (outside / "secret").write_text("s")
    (source / "f.txt").write_text("hello")
    (source / "run.sh").write_text("#!/bin/sh\n")
    os.chmod(source / "run.sh", 0o750)
    os.chmod(source / "d", 0o751)
    os.utime(source / "f.txt", ns=(1_000_000_000_000_000_000, 1_100_000_000_000_000_000))
    (source / "to-outside").symlink_to(outside)
    (source / "dangling").symlink_to(tmp_path / "missing")
    done = _copy(source, tmp_path / "dst")
    assert done.returncode == 0, done.stderr
    dst = tmp_path / "dst"
    assert (dst / "f.txt").read_text() == "hello"
    assert os.lstat(dst / "f.txt").st_mtime_ns == 1_100_000_000_000_000_000
    assert stat.S_IMODE(os.lstat(dst / "run.sh").st_mode) == 0o750
    assert stat.S_IMODE(os.lstat(dst / "d").st_mode) == 0o751
    for name in ("to-outside", "dangling"):
        assert os.path.islink(dst / name) and os.readlink(dst / name) == os.readlink(source / name)
    assert not (dst / "secret").exists()  # the symlinked directory was not copied into dst


def test_copy_refuses_a_fifo_without_opening_it(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    os.mkfifo(source / "pipe")
    done = _copy(source, tmp_path / "dst")  # an open() would block until the timeout
    assert done.returncode != 0 and "refused file type" in done.stderr


def test_copy_refuses_an_existing_target(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "dst").mkdir()
    done = _copy(tmp_path / "src", tmp_path / "dst")
    assert done.returncode != 0 and "target exists" in done.stderr


class _LocalAccount:
    """``wrap`` runs argv directly, so the copy is made by (and owned by) the test's own uid."""

    grant_read = staticmethod(VerifierAccount.grant_read)

    def __init__(self, user, root, tamper=""):
        self.user, self.root, self.tamper = user, root, tamper

    def wrap(self, argv, profile, env=None):
        assert profile is None
        return [*argv[:3], argv[3] + self.tamper, *argv[4:]]


def _make(repo, tmp_path, account):
    work = tmp_path / "w.work"
    work.mkdir()
    commit = git(repo, "rev-parse", "HEAD")
    return work, lambda: make_account_checkout(account, sys.executable, repo, commit, work, "w")


ME = pwd.getpwuid(os.getuid()).pw_name


def test_the_copy_is_the_candidate_tree_and_the_staging_clone_goes(repo, tmp_path):
    work, make = _make(repo, tmp_path, _LocalAccount(ME, tmp_path))
    checkout = make()
    assert checkout == work / "w" and (checkout / "src" / "a.py").read_text() == "x = 1\n"
    assert (checkout / ".git").is_dir()
    assert not (work / STAGE).exists()


def test_a_copy_not_owned_by_the_account_is_refused(repo, tmp_path):
    other = next(p.pw_name for p in pwd.getpwall() if p.pw_uid not in (0, os.getuid()))
    work, make = _make(repo, tmp_path, _LocalAccount(other, tmp_path))
    with pytest.raises(WorkspaceError, match="not a directory owned by"):
        make()
    assert not (work / STAGE).exists()


def test_a_copy_that_differs_from_the_candidate_is_refused(repo, tmp_path):
    tamper = "\nopen(os.path.join(target, 'src', 'a.py'), 'a').write('y = 2\\n')\n"
    work, make = _make(repo, tmp_path, _LocalAccount(ME, tmp_path, tamper))
    with pytest.raises(WorkspaceError, match="differs from"):
        make()
    assert not (work / STAGE).exists()


def test_a_failed_copy_is_an_error_and_the_staging_clone_goes(repo, tmp_path):
    work, make = _make(repo, tmp_path, _LocalAccount(ME, tmp_path, "\nsys.exit('boom')\n"))
    with pytest.raises(WorkspaceError, match="account copy failed"):
        make()
    assert not (work / STAGE).exists()
    assert Path(work).is_dir()  # the work dir itself is the caller's to remove
