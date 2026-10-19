"""Git-bound change set for one candidate commit (input to Core notarisation).

The manifest is read from git itself (``git diff-tree --raw``), never from what
a worker claims it changed. Renames are disabled so every path is an explicit
ADD, MODIFY or DELETE, which is what nexus-certify's manifest expects. A type
change (file <-> symlink, git status ``T``) is a MODIFY with a different mode.
Git runs with every inherited ``GIT_*`` variable removed, so a hook environment
cannot point it at another repository.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cairnwork.gitcmd import OID, GitCommandError, run_git

_ZERO_OID = "0" * 40
_ZERO_MODE = "000000"
_KINDS = {"A": "ADD", "M": "MODIFY", "D": "DELETE", "T": "MODIFY"}


class ChangeSetError(Exception):
    code = "CHANGE_SET_ERROR"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    change_type: str
    before_oid: str | None
    after_oid: str | None
    before_mode: str | None
    after_mode: str | None

    def to_core(self) -> dict[str, str | None]:
        return {
            "path": self.path,
            "change_type": self.change_type,
            "before_oid": self.before_oid,
            "after_oid": self.after_oid,
            "before_mode": self.before_mode,
            "after_mode": self.after_mode,
        }


@dataclass(frozen=True)
class GitChangeSet:
    base_commit: str
    candidate_commit: str
    base_tree: str
    candidate_tree: str
    entries: tuple[ManifestEntry, ...]

    @property
    def paths(self) -> list[str]:
        return sorted(entry.path for entry in self.entries)

    @property
    def deleted_paths(self) -> list[str]:
        return sorted(e.path for e in self.entries if e.change_type == "DELETE")


def read_change_set(repo: Path, base_commit: str, candidate_commit: str) -> GitChangeSet:
    """Read the change from base to candidate; both must be full commit ids."""
    for name, value in (("base_commit", base_commit), ("candidate_commit", candidate_commit)):
        if not OID.match(value):
            raise ChangeSetError(f"{name} must be a 40-hex commit id")
    if base_commit == candidate_commit:
        raise ChangeSetError("candidate_commit equals base_commit")
    base_tree = _tree_of(repo, base_commit)
    candidate_tree = _tree_of(repo, candidate_commit)
    raw = _git(
        repo, "diff-tree", "-r", "-z", "--raw", "--no-renames", base_commit, candidate_commit
    )
    entries = _parse_raw(raw)
    if not entries:
        raise ChangeSetError("candidate changes no files")
    return GitChangeSet(base_commit, candidate_commit, base_tree, candidate_tree, entries)


def _tree_of(repo: Path, commit: str) -> str:
    kind = _git(repo, "cat-file", "-t", commit).strip()
    if kind != "commit":
        raise ChangeSetError(f"{commit} is a {kind}, not a commit")
    tree = _git(repo, "rev-parse", f"{commit}^{{tree}}").strip()
    if not OID.match(tree):
        raise ChangeSetError(f"tree of {commit} is malformed")
    return tree


def _parse_raw(raw: str) -> tuple[ManifestEntry, ...]:
    fields = raw.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) % 2:
        raise ChangeSetError("unexpected git diff-tree output")
    entries = []
    for header, path in zip(fields[0::2], fields[1::2], strict=True):
        parts = header.lstrip(":").split(" ")
        if len(parts) != 5 or parts[4] not in _KINDS:
            raise ChangeSetError(f"unsupported change {header!r} for {path!r}")
        old_mode, new_mode, old_oid, new_oid, status = parts
        entries.append(
            ManifestEntry(
                path=path,
                change_type=_KINDS[status],
                before_oid=None if old_oid == _ZERO_OID else old_oid,
                after_oid=None if new_oid == _ZERO_OID else new_oid,
                before_mode=None if old_mode == _ZERO_MODE else old_mode,
                after_mode=None if new_mode == _ZERO_MODE else new_mode,
            )
        )
    return tuple(sorted(entries, key=lambda entry: entry.path))


def _git(repo: Path, *args: str) -> str:
    try:
        return run_git(repo, *args)
    except GitCommandError as exc:
        raise ChangeSetError(f"git {args[0]} failed") from exc
