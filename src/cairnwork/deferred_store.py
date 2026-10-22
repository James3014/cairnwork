"""Where the deferred tests live: a private store the worker cannot reach (Issue #119).

A test commit used to be pushed to a public ``cwt-*`` branch of the target repository, so a
worker with network access could fetch it whatever its OS account could read. The store keeps
it as a single-commit ``git bundle`` under ``<state>/deferred-tests/``: the state directory is
mode 0700 owned by the account that runs Cairnwork (``Intake.prepare``), so neither the worker
account nor any other user can enter it, and no remote holds the commit. Cairnwork fetches the
bundle into its own clone only after the worker is done (``deferred_tests.ensure_test_commit``).

``import_bundle`` re-creates the bundle in a canonical shape (one head, ``refs/cairnwork/test``,
one parent, prerequisite = that parent) inside a private scratch repository, so what is stored
is what Cairnwork built, not what the supplier sent. Nothing is overwritten and a commit that
is already on the mirror or already burned (``verify-used-tests``) is refused.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cairnwork.deferred_tests import TestCommitRejected
from cairnwork.gitcmd import OID, GitCommandError, run_git

HEAD_REF = "refs/cairnwork/test"


@dataclass(frozen=True)
class DeferredStore:
    path: Path
    required: bool = False  # True: a test commit that is not in the store is refused

    def check_dir(self) -> None:
        try:
            info = self.path.lstat()
        except OSError as exc:
            raise TestCommitRejected("TEST_STORE_UNSAFE", f"{self.path}: {exc}") from exc
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise TestCommitRejected("TEST_STORE_UNSAFE", f"{self.path} is not our directory")
        if info.st_mode & 0o077:
            raise TestCommitRejected("TEST_STORE_UNSAFE", f"{self.path} must be mode 0700")

    def bundle(self, sha: str) -> Path:
        if not OID.fullmatch(sha):
            raise TestCommitRejected("TEST_COMMIT_INVALID", "not a 40-hex commit id")
        return self.path / f"{sha}.bundle"

    def has(self, sha: str) -> bool:
        path = self.bundle(sha)
        return path.is_file() and not path.is_symlink()

    def parent(self, sha: str, repo: Path) -> str:
        """The test commit's only parent, read from the stored bundle (repo holds the base)."""
        self.check_dir()
        if not self.has(sha):
            raise TestCommitRejected("TEST_COMMIT_NOT_IMPORTED", sha)
        with _Scratch(self.path, repo) as scratch:
            try:
                run_git(
                    scratch,
                    "fetch",
                    "--quiet",
                    "--no-tags",
                    str(self.bundle(sha)),
                    f"{HEAD_REF}:{HEAD_REF}",
                )
            except GitCommandError as exc:
                raise TestCommitRejected("TEST_COMMIT_UNREADABLE", str(exc)) from exc
            return _only_parent(scratch)

    def fetch_into(self, repo: Path, sha: str) -> None:
        """Bring the stored commit into repo (the verifier's clone), after the worker."""
        self.check_dir()
        if not self.has(sha):
            raise TestCommitRejected("TEST_COMMIT_NOT_IMPORTED", sha)
        try:
            run_git(repo, "fetch", "--quiet", "--no-tags", str(self.bundle(sha)), HEAD_REF)
            run_git(repo, "cat-file", "-e", f"{sha}^{{commit}}")
        except GitCommandError as exc:
            raise TestCommitRejected("TEST_COMMIT_UNREADABLE", str(exc)) from exc

    def import_bundle(self, repo: Path, bundle: Path, used: Path) -> str:
        """Validate a supplied bundle and store it canonically; return the test commit id."""
        self.check_dir()
        with _Scratch(self.path, repo) as scratch:
            try:
                lines = run_git(scratch, "bundle", "list-heads", str(bundle)).split("\n")
                heads = [h.split() for h in lines if h.strip()]
                if len(heads) != 1 or not OID.fullmatch(heads[0][0]):
                    raise TestCommitRejected("TEST_BUNDLE_SHAPE", "exactly one head is required")
                sha, ref = heads[0][0], heads[0][1]
                if not ref.startswith("refs/") and ref != "HEAD":
                    raise TestCommitRejected("TEST_BUNDLE_SHAPE", "unexpected head name")
                run_git(scratch, "fetch", "--quiet", "--no-tags", str(bundle), f"{ref}:{HEAD_REF}")
                if run_git(scratch, "cat-file", "-t", sha).strip() != "commit":
                    raise TestCommitRejected("TEST_BUNDLE_SHAPE", "the head is not a commit")
                parent = _only_parent(scratch)
                try:  # the base must already be the mirror's own, not carried by the bundle
                    run_git(repo, "cat-file", "-e", f"{parent}^{{commit}}")
                except GitCommandError:
                    raise TestCommitRejected(
                        "TEST_COMMIT_NOT_ON_BASE", "the parent is not in the mirror"
                    ) from None
                self._reject_known(repo, sha, used)
                fd, tmp = tempfile.mkstemp(dir=self.path, prefix=".import-", suffix=".bundle")
                os.close(fd)
                try:
                    run_git(scratch, "bundle", "create", "-q", tmp, HEAD_REF, f"^{parent}")
                    os.chmod(tmp, 0o600)  # git replaced the file: set the mode after
                    os.link(tmp, self.bundle(sha))  # fails if one is already stored
                finally:
                    os.unlink(tmp)
            except FileExistsError:
                raise TestCommitRejected("TEST_BUNDLE_EXISTS", "already imported") from None
            except GitCommandError as exc:
                raise TestCommitRejected("TEST_COMMIT_UNREADABLE", str(exc)) from exc
        return sha

    @staticmethod
    def _reject_known(repo: Path, sha: str, used: Path) -> None:
        if (used / sha).exists():
            raise TestCommitRejected("TEST_COMMIT_ALREADY_EXPOSED", "burned by an earlier run")
        try:
            run_git(repo, "cat-file", "-e", f"{sha}^{{commit}}")
        except GitCommandError:
            return
        raise TestCommitRejected("TEST_COMMIT_ALREADY_EXPOSED", "already in the mirror")


class _Scratch:
    """A throwaway bare repository inside the store (owner-only); with ``repo`` it borrows
    that repository's objects (alternates), so a bundle's prerequisite can be resolved."""

    def __init__(self, parent: Path, repo: Path | None) -> None:
        self.parent, self.repo = parent, repo
        self.dir = parent

    def __enter__(self) -> Path:
        self.dir = Path(tempfile.mkdtemp(dir=self.parent, prefix=".scratch-"))
        try:
            run_git(self.dir, "init", "--bare", "--quiet")
            if self.repo is not None:
                objects = self.repo / ".git" / "objects"
                objects = objects if objects.is_dir() else self.repo / "objects"
                info = self.dir / "objects" / "info"
                info.mkdir(parents=True, exist_ok=True)
                (info / "alternates").write_text(f"{objects}\n")
        except (GitCommandError, OSError):
            shutil.rmtree(self.dir, ignore_errors=True)
            raise
        return self.dir

    def __exit__(self, *_exc: object) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def _only_parent(scratch: Path) -> str:
    fields = run_git(scratch, "rev-list", "--parents", "-n", "1", HEAD_REF).split()
    if len(fields) != 2 or not OID.fullmatch(fields[1]):
        raise TestCommitRejected("TEST_COMMIT_NOT_ON_BASE", "its only parent must be the base")
    return fields[1]
