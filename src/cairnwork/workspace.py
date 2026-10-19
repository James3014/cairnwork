"""Isolated attempt worktrees and candidate checks (preserved behaviors #11-#13, #19).

Every attempt runs in a fresh detached git worktree at the task's base commit.
After the worker stops, ``check_candidate`` decides from git alone whether the
worktree holds an acceptable candidate:

- exactly one new commit whose only parent is the base (#19);
- a clean working tree, so nothing uncommitted is silently dropped;
- every changed path is in allowed_files, deletions only when allowed, and no
  symlinks or submodules are added or changed (#12);
- ``git diff --check`` finds no whitespace errors (#13).

Any other state is a typed CandidateRejected; nothing is repaired or guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cairnwork.changeset import ChangeSetError, read_change_set
from cairnwork.gitcmd import OID, GitCommandError, run_git

FORBIDDEN_MODES = frozenset({"120000", "160000"})  # symlink, submodule


class WorkspaceError(Exception):
    code = "WORKSPACE_ERROR"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


class CandidateRejected(Exception):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class Candidate:
    base_commit: str
    candidate_commit: str
    paths: tuple[str, ...]


def create_attempt_worktree(repo: Path, base_commit: str, root: Path, attempt_id: str) -> Path:
    """Create root/attempt_id as a detached worktree at base_commit; never reuse a path."""
    if not OID.match(base_commit):
        raise WorkspaceError("base_commit must be a 40-hex commit id")
    if not attempt_id or "/" in attempt_id or attempt_id in {".", ".."}:
        raise WorkspaceError(f"attempt_id {attempt_id!r} is not a single path component")
    path = Path(root) / attempt_id
    if path.exists() or path.is_symlink():
        raise WorkspaceError(f"{path} already exists")
    try:
        if run_git(repo, "cat-file", "-t", base_commit).strip() != "commit":
            raise WorkspaceError(f"{base_commit} is not a commit")
        run_git(repo, "worktree", "add", "--detach", str(path), base_commit)
        head = run_git(path, "rev-parse", "HEAD").strip()
        status = run_git(path, "status", "--porcelain", "--untracked-files=all")
    except GitCommandError as exc:
        raise WorkspaceError(str(exc)) from exc
    if head != base_commit or status:
        raise WorkspaceError(f"{path} is not a clean checkout of {base_commit}")
    return path


def remove_attempt_worktree(repo: Path, path: Path) -> None:
    try:
        run_git(repo, "worktree", "remove", "--force", str(path))
    except GitCommandError as exc:
        raise WorkspaceError(str(exc)) from exc


def check_candidate(
    worktree: Path,
    base_commit: str,
    allowed_files: tuple[str, ...],
    *,
    allow_deletions: bool = False,
) -> Candidate:
    """Accept the worktree's HEAD as a candidate, or raise CandidateRejected with a code."""
    head, parents, status = _head_state(worktree)
    if head == base_commit:
        raise CandidateRejected("NO_COMMIT", "the worker made no commit")
    if parents != [base_commit]:
        raise CandidateRejected("NOT_ONE_COMMIT_ON_BASE", f"parents of {head} are {parents}")
    if status:
        raise CandidateRejected("DIRTY_TREE", "uncommitted or untracked changes remain")
    try:
        change = read_change_set(worktree, base_commit, head)
    except ChangeSetError as exc:
        raise CandidateRejected("UNREADABLE_CHANGE", str(exc)) from exc
    allowed = set(allowed_files)
    for entry in change.entries:
        if entry.path not in allowed:
            raise CandidateRejected("OUT_OF_SCOPE", entry.path)
        if entry.change_type == "DELETE" and not allow_deletions:
            raise CandidateRejected("DELETION_FORBIDDEN", entry.path)
        if entry.after_mode in FORBIDDEN_MODES or entry.before_mode in FORBIDDEN_MODES:
            raise CandidateRejected("FORBIDDEN_MODE", f"{entry.path} ({entry.after_mode})")
    _require_clean_diff(worktree, base_commit, head)
    return Candidate(base_commit, head, tuple(change.paths))


def _head_state(worktree: Path) -> tuple[str, list[str], str]:
    try:
        head = run_git(worktree, "rev-parse", "HEAD").strip()
        parents = run_git(worktree, "rev-list", "--parents", "-n", "1", head).split()[1:]
        status = run_git(worktree, "status", "--porcelain", "--untracked-files=all")
    except GitCommandError as exc:
        raise CandidateRejected("UNREADABLE_WORKTREE", str(exc)) from exc
    return head, parents, status


def _require_clean_diff(worktree: Path, base_commit: str, head: str) -> None:
    try:
        run_git(worktree, "diff", "--check", base_commit, head)
    except GitCommandError as exc:
        raise CandidateRejected("DIFF_CHECK_FAILED", "whitespace errors in the change") from exc
