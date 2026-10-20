"""Turn a worker's edited files into a candidate commit, without running git in the clone.

Some workers (for example an editing agent) change files but do not commit.
Running ``git add``/``git commit`` inside the worker's clone would read the
clone's config and attributes, which the worker controls. Instead Cairnwork
builds the commit in its own repository: it reads each allowed file from the
clone (opened with O_NOFOLLOW; regular files with one link only, no symlinks or
hard links to files elsewhere), stores the bytes it read with ``hash-object
--no-filters``, builds a tree on top of the base in a private index, and
commits it with a fixed identity. The commit is then checked like any other
candidate (``check_candidate_commit``), which also compares the whole clone with
the new tree, so a change to any other file makes the candidate DIRTY_TREE.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from cairnwork.gitcmd import GitCommandError, hash_blob, run_git
from cairnwork.workspace import (
    ATTEMPT_REF_PREFIX,
    Attempt,
    Candidate,
    CandidateRejected,
    check_candidate_commit,
    require_refs_unchanged,
)

_IDENTITY = ("-c", "user.name=Cairnwork", "-c", "user.email=cairnwork@localhost")


def snapshot_candidate(
    repo: Path,
    attempt: Attempt,
    allowed_files: tuple[str, ...],
    *,
    allow_deletions: bool = False,
) -> Candidate:
    """Commit the clone's allowed files on top of the base, then check the result."""
    require_refs_unchanged(repo, attempt)
    index = attempt.path.parent / f"{attempt.attempt_id}.index"
    if index.exists() or index.is_symlink():
        raise CandidateRejected("UNREADABLE_WORKTREE", f"{index} already exists")
    try:
        run_git(repo, "read-tree", attempt.base_commit, index_file=index)
        for path in sorted(set(allowed_files)):
            _stage(repo, attempt.path, path, index)
        tree = run_git(repo, "write-tree", index_file=index).strip()
        base_tree = run_git(repo, "rev-parse", f"{attempt.base_commit}^{{tree}}").strip()
        if tree == base_tree:
            raise CandidateRejected("NO_COMMIT", "the worker changed no allowed file")
        message = f"Cairnwork attempt {attempt.attempt_id}"
        head = run_git(
            repo, *_IDENTITY, "commit-tree", tree, "-p", attempt.base_commit, "-m", message
        ).strip()
        run_git(repo, "update-ref", ATTEMPT_REF_PREFIX + attempt.attempt_id, head)
    except GitCommandError as exc:
        raise CandidateRejected("UNREADABLE_WORKTREE", str(exc)) from exc
    finally:
        index.unlink(missing_ok=True)
    return check_candidate_commit(
        repo, attempt, head, allowed_files, allow_deletions=allow_deletions
    )


def _stage(repo: Path, clone: Path, path: str, index: Path) -> None:
    full = clone / path
    parent = clone
    for part in Path(path).parts[:-1]:
        parent = parent / part
        if parent.is_symlink():
            raise CandidateRejected("FORBIDDEN_MODE", f"{path}: {part} is a symlink")
    try:
        fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        run_git(repo, "update-index", "--force-remove", "--", path, index_file=index)
        return
    except OSError as exc:  # a symlink (ELOOP), FIFO, unreadable file, ...
        raise CandidateRejected("FORBIDDEN_MODE", f"{path}: {exc.strerror}") from exc
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise CandidateRejected("FORBIDDEN_MODE", f"{path} is not a regular file")
        if info.st_nlink != 1:
            raise CandidateRejected("FORBIDDEN_MODE", f"{path} is a hard link")
        data = handle.read()
    oid = hash_blob(repo, data)
    mode = "100755" if info.st_mode & stat.S_IXUSR else "100644"
    run_git(repo, "update-index", "--add", "--cacheinfo", f"{mode},{oid},{path}", index_file=index)
