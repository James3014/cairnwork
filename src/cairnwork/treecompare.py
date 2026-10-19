"""Compare a directory on disk with a commit's tree, without running git in it.

Used to decide whether a worker's clone holds exactly its candidate commit. The
walk never follows symlinks, never reads a file whose size differs from the
expected blob, hashes in chunks, and fails closed on anything it cannot read:
an unreadable directory, file or special file is reported, never skipped.
Gitlink (submodule) paths must be empty directories, and a directory that no
tracked path lives under is a difference.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from cairnwork.gitcmd import run_git

CHUNK = 1 << 20


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    oid: str
    size: int | None


def tree_entries(repo: Path, commit: str) -> dict[str, TreeEntry]:
    raw = run_git(repo, "ls-tree", "-r", "-z", "-l", "--full-tree", commit)
    entries = {}
    for record in filter(None, raw.split("\0")):
        meta, path = record.split("\t", 1)
        mode, _kind, oid, size = meta.split()
        entries[path] = TreeEntry(mode, oid, None if size == "-" else int(size))
    return entries


def tree_differences(repo: Path, commit: str, directory: Path) -> list[str]:
    """Paths where directory differs from commit's tree (the top-level .git is ignored)."""
    expected = tree_entries(repo, commit)
    expected_dirs = {str(Path(p).parent) for p in expected} | {"."}
    for path in list(expected_dirs):
        expected_dirs.update(str(parent) for parent in Path(path).parents)
    differences: list[str] = []
    seen: set[str] = set()
    try:
        _compare(Path(directory), expected, expected_dirs, seen, differences)
    except OSError as exc:
        return [f"unreadable {exc.filename or exc}"]
    differences.extend(f"missing {rel}" for rel in expected if rel not in seen)
    return sorted(differences)


def _compare(top, expected, expected_dirs, seen, differences) -> None:
    for current, dirnames, filenames in os.walk(top, onerror=_raise, followlinks=False):
        rel_dir = Path(current).relative_to(top).as_posix()
        if rel_dir == ".":
            dirnames[:] = [d for d in dirnames if d != ".git"]
            filenames = [f for f in filenames if f != ".git"]
        for name in sorted(dirnames):
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            full = Path(current) / name
            entry = expected.get(rel)
            if full.is_symlink():
                dirnames.remove(name)
                seen.add(rel)
                if not _same(entry, "120000", os.readlink(full).encode()):
                    differences.append(f"changed {rel}")
            elif entry is not None and entry.mode == "160000":
                dirnames.remove(name)
                seen.add(rel)
                if any(full.iterdir()):
                    differences.append(f"non-empty submodule {rel}")
            elif rel not in expected_dirs:
                differences.append(f"extra directory {rel}")
        for name in filenames:
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            seen.add(rel)
            if not _file_matches(Path(current) / name, expected.get(rel)):
                differences.append(f"{'changed' if rel in expected else 'extra'} {rel}")


def _file_matches(path: Path, entry: TreeEntry | None) -> bool:
    if entry is None:
        return False
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return _same(entry, "120000", os.readlink(path).encode())
    if not stat.S_ISREG(info.st_mode):
        return False
    mode = "100755" if info.st_mode & stat.S_IXUSR else "100644"
    if mode != entry.mode or info.st_size != entry.size:
        return False
    digest = hashlib.sha1(b"blob %d\0" % info.st_size, usedforsecurity=False)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode) or opened.st_size != entry.size:
            return False  # swapped between lstat and open
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest() == entry.oid


def _same(entry: TreeEntry | None, mode: str, data: bytes) -> bool:
    if entry is None or entry.mode != mode:
        return False
    oid = hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()
    return oid == entry.oid


def _raise(error: OSError) -> None:
    raise error
