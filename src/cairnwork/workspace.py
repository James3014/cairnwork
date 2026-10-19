"""Isolated attempt clones and candidate checks (preserved behaviors #11-#13, #19).

A worker never gets a checkout that shares Cairnwork's repository. Each attempt
is a separate ``git clone --no-local`` with its own ``.git`` (config, hooks,
refs, index, info/), detached at the task's base commit. Whatever the worker
does to that repository cannot reach Cairnwork's.

After the worker stops, ``collect_candidate`` fetches the clone's HEAD into
Cairnwork's repository under ``refs/cairnwork/attempts/<attempt_id>`` and decides
from Cairnwork's own repository whether it is acceptable:

- exactly one new commit whose only parent is the base (#19);
- every changed path is in allowed_files, deletions only when allowed, and no
  symlinks or submodules are added or changed (#12);
- ``git diff --check`` finds no whitespace errors (#13);
- the clone's files are exactly the candidate's tree. This is compared in
  Python, file by file (content hash, executable bit, symlink target), so no
  git config, index flag (skip-worktree, assume-unchanged), exclude file or
  ``core.worktree`` in the clone can hide a leftover change.

The only git command that touches the worker's clone is that fetch (git runs
upload-pack in it). Any other state is a typed CandidateRejected.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from cairnwork.changeset import ChangeSetError, read_change_set
from cairnwork.gitcmd import OID, GitCommandError, run_git

FORBIDDEN_MODES = frozenset({"120000", "160000"})  # symlink, submodule
ATTEMPT_REF_PREFIX = "refs/cairnwork/attempts/"


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


def create_attempt_clone(repo: Path, base_commit: str, root: Path, attempt_id: str) -> Path:
    """Clone repo into root/attempt_id, detached at base_commit; never reuse a path."""
    if not OID.match(base_commit):
        raise WorkspaceError("base_commit must be a 40-hex commit id")
    path = _attempt_path(root, attempt_id)
    if path.exists() or path.is_symlink():
        raise WorkspaceError(f"{path} already exists")
    try:
        if run_git(repo, "cat-file", "-t", base_commit).strip() != "commit":
            raise WorkspaceError(f"{base_commit} is not a commit")
        path.parent.mkdir(parents=True, exist_ok=True)
        run_git(
            path.parent, "clone", "--quiet", "--no-local", "--no-checkout", str(repo), path.name
        )
        run_git(path, "checkout", "--quiet", "--detach", base_commit)
        head = run_git(path, "rev-parse", "HEAD").strip()
    except GitCommandError as exc:
        raise WorkspaceError(str(exc)) from exc
    if head != base_commit or _tree_differences(repo, base_commit, path):
        raise WorkspaceError(f"{path} is not a clean checkout of {base_commit}")
    return path


def remove_attempt_clone(root: Path, attempt_id: str) -> None:
    path = _attempt_path(root, attempt_id)
    if path.is_symlink() or not path.is_dir():
        raise WorkspaceError(f"{path} is not an attempt directory")
    shutil.rmtree(path)


def collect_candidate(
    repo: Path,
    clone: Path,
    attempt_id: str,
    base_commit: str,
    allowed_files: tuple[str, ...],
    *,
    allow_deletions: bool = False,
) -> Candidate:
    """Fetch the clone's HEAD into repo and accept it, or raise CandidateRejected."""
    head = _fetch_head(repo, clone, attempt_id)
    if head == base_commit:
        raise CandidateRejected("NO_COMMIT", "the worker made no commit")
    parents = _parents(repo, head)
    if parents != [base_commit]:
        raise CandidateRejected("NOT_ONE_COMMIT_ON_BASE", f"parents of {head} are {parents}")
    try:
        change = read_change_set(repo, base_commit, head)
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
    try:
        run_git(repo, "diff", "--no-ext-diff", "--no-textconv", "--check", base_commit, head)
    except GitCommandError as exc:
        raise CandidateRejected("DIFF_CHECK_FAILED", "whitespace errors in the change") from exc
    differences = _tree_differences(repo, head, clone)
    if differences:
        raise CandidateRejected("DIRTY_TREE", ", ".join(differences[:5]))
    return Candidate(base_commit, head, tuple(change.paths))


def _attempt_path(root: Path, attempt_id: str) -> Path:
    if not attempt_id or "/" in attempt_id or attempt_id in {".", ".."}:
        raise WorkspaceError(f"attempt_id {attempt_id!r} is not a single path component")
    return Path(root) / attempt_id


def _fetch_head(repo: Path, clone: Path, attempt_id: str) -> str:
    ref = ATTEMPT_REF_PREFIX + attempt_id
    try:
        run_git(
            repo,
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-recurse-submodules",
            str(clone),
            f"+HEAD:{ref}",
        )
        head = run_git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    except GitCommandError as exc:
        raise CandidateRejected("UNREADABLE_WORKTREE", str(exc)) from exc
    if not OID.match(head):
        raise CandidateRejected("UNREADABLE_WORKTREE", f"HEAD {head!r} is malformed")
    return head


def _parents(repo: Path, commit: str) -> list[str]:
    try:
        return run_git(repo, "rev-list", "--parents", "-n", "1", commit).split()[1:]
    except GitCommandError as exc:
        raise CandidateRejected("UNREADABLE_CHANGE", str(exc)) from exc


def _tree_differences(repo: Path, commit: str, directory: Path) -> list[str]:
    """Paths where directory's files differ from commit's tree (ignoring the top .git)."""
    expected = _tree_entries(repo, commit)
    differences = []
    seen = set()
    for rel, kind, data in _walk(
        directory, gitlinks={p for p, (m, _) in expected.items() if m == "160000"}
    ):
        seen.add(rel)
        mode_oid = expected.get(rel)
        if mode_oid is None:
            differences.append(f"extra {rel}")
        elif mode_oid[0] == "160000":
            continue
        elif (kind, _blob_oid(data)) != (mode_oid[0], mode_oid[1]):
            differences.append(f"changed {rel}")
    differences.extend(f"missing {rel}" for rel in expected if rel not in seen)
    return sorted(differences)


def _tree_entries(repo: Path, commit: str) -> dict[str, tuple[str, str]]:
    raw = run_git(repo, "ls-tree", "-r", "-z", "--full-tree", commit)
    entries = {}
    for record in filter(None, raw.split("\0")):
        meta, path = record.split("\t", 1)
        mode, _kind, oid = meta.split(" ")
        entries[path] = (mode, oid)
    return entries


def _walk(directory: Path, gitlinks: set[str]):
    top = Path(directory)
    for current, dirnames, filenames in os.walk(top, followlinks=False):
        rel_dir = Path(current).relative_to(top).as_posix()
        if rel_dir == ".":
            dirnames[:] = [d for d in dirnames if d != ".git"]
            filenames = [f for f in filenames if f != ".git"]
        for name in sorted(dirnames):
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            full = Path(current) / name
            if rel in gitlinks:
                dirnames.remove(name)
                yield rel, "160000", b""
            elif full.is_symlink():
                dirnames.remove(name)
                yield rel, "120000", os.readlink(full).encode()
        for name in filenames:
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            yield (rel, *_file_kind_and_data(Path(current) / name))


def _file_kind_and_data(path: Path) -> tuple[str, bytes]:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return "120000", os.readlink(path).encode()
    if not stat.S_ISREG(info.st_mode):
        return "special", b""
    mode = "100755" if info.st_mode & stat.S_IXUSR else "100644"
    return mode, path.read_bytes()


def _blob_oid(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()
