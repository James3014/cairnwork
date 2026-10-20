"""Isolated attempt clones and candidate checks (preserved behaviors #11-#13, #19).

Each attempt is a separate ``git clone --no-local`` with its own ``.git``
(objects, config, hooks, refs, index, info/), detached at the task's base
commit, with its ``origin`` remote removed so git gives the worker no handle
on Cairnwork's repository. Cairnwork's repository path must still not be
writable from the worker's sandbox: git cannot stop a process that already
has write access, and branch and tag protection relies on that. As a check on
the refs that matter for verification, ``collect_candidate`` refuses a
candidate when ``refs/replace/`` or another attempt's ``refs/cairnwork/`` ref
changed during the attempt; ordinary branch updates by Cairnwork are allowed.

The worker process must have exited before ``collect_candidate`` runs, and the
clone must not change afterwards: the checks describe the clone at that moment.

After the worker stops, ``collect_candidate`` fetches the clone's HEAD into
``refs/cairnwork/attempts/<attempt_id>`` and decides from Cairnwork's own
repository whether it is acceptable:

- exactly one new commit whose only parent is the base (#19);
- every changed path is in allowed_files, deletions only when allowed, and no
  symlinks or submodules are added or changed (#12);
- ``git diff --check`` finds no whitespace errors (#13);
- the clone's files are exactly the candidate's tree, compared in Python
  (see treecompare.py), so no git state in the clone can hide a leftover.

The only git command that touches the clone after the worker starts is that
fetch (git runs upload-pack in it). Any other state is a typed CandidateRejected.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from cairnwork.changeset import ChangeSetError, read_change_set
from cairnwork.gitcmd import OID, GitCommandError, run_git
from cairnwork.treecompare import tree_differences

FORBIDDEN_MODES = frozenset({"120000", "160000"})  # symlink, submodule
ATTEMPT_REF_PREFIX = "refs/cairnwork/attempts/"
ATTEMPT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class WorkspaceError(Exception):
    code = "WORKSPACE_ERROR"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


class CandidateRejected(Exception):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    base_commit: str
    path: Path
    refs_before: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Candidate:
    base_commit: str
    candidate_commit: str
    paths: tuple[str, ...]


def create_attempt_clone(repo: Path, base_commit: str, root: Path, attempt_id: str) -> Attempt:
    """Clone repo into root/attempt_id, detached at base_commit; never reuse a path."""
    if not OID.fullmatch(base_commit):
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
        run_git(path, "remote", "remove", "origin")
        try:
            run_git(path, "cat-file", "-e", f"{base_commit}^{{commit}}")
        except GitCommandError:  # e.g. a candidate only reachable from refs/cairnwork/
            run_git(path, "fetch", "--quiet", "--no-tags", str(repo), base_commit)
        run_git(path, "checkout", "--quiet", "--detach", base_commit)
        _restrict_to_base_history(path, base_commit)
        head = run_git(path, "rev-parse", "HEAD").strip()
        refs_before = _guarded_refs(repo, attempt_id)
    except (GitCommandError, OSError) as exc:
        raise WorkspaceError(str(exc)) from exc
    try:
        differences = tree_differences(repo, base_commit, path)
    except GitCommandError as exc:
        raise WorkspaceError(str(exc)) from exc
    if head != base_commit or differences:
        raise WorkspaceError(f"{path} is not a clean checkout of {base_commit}")
    return Attempt(attempt_id, base_commit, path, refs_before)


def _restrict_to_base_history(path: Path, base_commit: str) -> None:
    """Leave the worker only base's history. ``git clone`` copies every object and keeps
    a branch at the source tip, so when base is not that tip a worker can read a later
    "answer" commit — or a tag or blob with the answer — with ``git log``/``git show``
    /``git cat-file`` (preserved behavior #11). Delete every ref, expire reflogs, and drop
    unreachable objects (``gc.bigPackThreshold=0`` so a large pack is not kept), then fail
    closed if ANY object — commit, tree, blob or tag — outside base's history survives.
    The gc runs with the operator's global/system config ignored, so a configured
    ``gc.recentObjectsHook`` command cannot run. The detached HEAD keeps base reachable,
    so the fetch-back in ``collect_candidate`` still works."""
    for ref in run_git(path, "for-each-ref", "--format=%(refname)").splitlines():
        run_git(path, "update-ref", "-d", ref)
    run_git(path, "reflog", "expire", "--expire=now", "--all", isolate_config=True)
    run_git(
        path, "-c", "gc.bigPackThreshold=0", "gc", "--prune=now", "--quiet", isolate_config=True
    )
    reachable = {
        line.split()[0] for line in run_git(path, "rev-list", "--objects", base_commit).splitlines()
    }
    present = run_git(
        path, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)"
    ).split()
    if set(present) - reachable:
        raise WorkspaceError(f"{path} still holds objects outside base history")


def remove_attempt_clone(root: Path, attempt_id: str) -> None:
    path = _attempt_path(root, attempt_id)
    if path.is_symlink() or not path.is_dir():
        raise WorkspaceError(f"{path} is not an attempt directory")
    try:
        _make_tree_writable(path)
        shutil.rmtree(path)
    except OSError as exc:
        raise WorkspaceError(f"could not remove {path}: {exc}") from exc


def collect_candidate(
    repo: Path,
    attempt: Attempt,
    allowed_files: tuple[str, ...],
    *,
    allow_deletions: bool = False,
) -> Candidate:
    """Fetch the clone's HEAD into repo and accept it, or raise CandidateRejected."""
    require_refs_unchanged(repo, attempt)
    head = _fetch_head(repo, attempt)
    return check_candidate_commit(
        repo, attempt, head, allowed_files, allow_deletions=allow_deletions
    )


def check_candidate_commit(
    repo: Path,
    attempt: Attempt,
    head: str,
    allowed_files: tuple[str, ...],
    *,
    allow_deletions: bool = False,
) -> Candidate:
    """Accept head (already in repo) as the attempt's candidate, or raise CandidateRejected."""
    base = attempt.base_commit
    if head == base:
        raise CandidateRejected("NO_COMMIT", "the worker made no commit")
    parents = _parents(repo, head)
    if parents != [base]:
        raise CandidateRejected("NOT_ONE_COMMIT_ON_BASE", f"parents of {head} are {parents}")
    try:
        change = read_change_set(repo, base, head)
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
        run_git(repo, "diff", "--no-ext-diff", "--no-textconv", "--check", base, head)
    except GitCommandError as exc:
        raise CandidateRejected("DIFF_CHECK_FAILED", "whitespace errors in the change") from exc
    try:
        differences = tree_differences(repo, head, attempt.path)
    except GitCommandError as exc:
        raise CandidateRejected("UNREADABLE_CHANGE", str(exc)) from exc
    if differences:
        raise CandidateRejected("DIRTY_TREE", ", ".join(differences[:5]))
    return Candidate(base, head, tuple(change.paths))


def _attempt_path(root: Path, attempt_id: str) -> Path:
    if not isinstance(attempt_id, str) or not ATTEMPT_ID.fullmatch(attempt_id):
        raise WorkspaceError(f"attempt_id {attempt_id!r} must match {ATTEMPT_ID.pattern}")
    return Path(root) / attempt_id


GUARDED_REF_PREFIXES = ("refs/replace/", "refs/cairnwork/")


def _guarded_refs(repo: Path, attempt_id: str) -> tuple[tuple[str, str], ...]:
    own = ATTEMPT_REF_PREFIX + attempt_id
    lines = run_git(
        repo, "for-each-ref", "--format=%(refname) %(objectname)", *GUARDED_REF_PREFIXES
    ).splitlines()
    pairs = (tuple(line.split(" ", 1)) for line in lines)
    return tuple(sorted((name, oid) for name, oid in pairs if name != own))


def _refs_acceptable(before: dict[str, str], now: dict[str, str]) -> bool:
    """refs/replace/ and non-attempt refs must be identical; other attempts may only add refs."""
    for name in before.keys() | now.keys():
        added = name not in before
        if name.startswith(ATTEMPT_REF_PREFIX) and added:
            continue  # another attempt was collected meanwhile
        if before.get(name) != now.get(name):
            return False
    return True


def require_refs_unchanged(repo: Path, attempt: Attempt) -> None:
    try:
        now = _guarded_refs(repo, attempt.attempt_id)
    except GitCommandError as exc:
        raise CandidateRejected("UNREADABLE_REPOSITORY", str(exc)) from exc
    if not _refs_acceptable(dict(attempt.refs_before), dict(now)):
        raise CandidateRejected(
            "REPOSITORY_REFS_CHANGED", "guarded refs changed during the attempt"
        )


def _fetch_head(repo: Path, attempt: Attempt) -> str:
    ref = ATTEMPT_REF_PREFIX + attempt.attempt_id
    try:
        run_git(
            repo,
            "fetch",
            "--quiet",
            "--no-tags",
            "--no-recurse-submodules",
            str(attempt.path),
            f"+HEAD:{ref}",
        )
        head = run_git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    except GitCommandError as exc:
        raise CandidateRejected("UNREADABLE_WORKTREE", str(exc)) from exc
    if not OID.fullmatch(head):
        raise CandidateRejected("UNREADABLE_WORKTREE", f"HEAD {head!r} is malformed")
    return head


def _parents(repo: Path, commit: str) -> list[str]:
    try:
        return run_git(repo, "rev-list", "--parents", "-n", "1", commit).split()[1:]
    except GitCommandError as exc:
        raise CandidateRejected("UNREADABLE_CHANGE", str(exc)) from exc


def _make_tree_writable(path: Path) -> None:
    """A worker may chmod directories away; give them back before removal (no symlinks)."""
    path.chmod(stat.S_IRWXU)
    for current, dirnames, _ in os.walk(path, followlinks=False):
        for name in dirnames:
            child = Path(current) / name
            if not child.is_symlink():
                child.chmod(stat.S_IRWXU)
