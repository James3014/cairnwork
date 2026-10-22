"""Rank a checkout's tracked text files against an Issue text with BM25."""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

from .bm25 import BM25Index, tokens

MAX_FILE_BYTES = 1024 * 1024
PATH_WEIGHT = 3  # a path token counts as this many content occurrences
_SNIFF_BYTES = 8192
_TEST_DIRS = frozenset({"test", "tests", "testing", "spec", "specs", "__tests__"})


def is_test_path(path: str) -> bool:
    """True for files under a test directory or named like a test module."""
    p = PurePosixPath(path)
    name = p.name.lower()
    return (
        any(part.lower() in _TEST_DIRS for part in p.parts[:-1])
        or name.startswith("test_")
        or p.stem.lower().endswith(("_test", "_tests"))
        or name in {"conftest.py", "tests.py"}
    )


def tracked_files(repo_dir: str | Path) -> list[str]:
    """Sorted repo-relative paths from ``git ls-files`` (untracked files never appear)."""
    out = subprocess.run(
        ["git", "-C", str(repo_dir), "ls-files", "-z"],
        capture_output=True,
        check=True,
    ).stdout
    return sorted(p for p in out.decode("utf-8", "surrogateescape").split("\0") if p)


def read_text(path: Path) -> str | None:
    """File text, or None for non-regular, oversized, binary or unreadable files."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > MAX_FILE_BYTES or b"\0" in data[:_SNIFF_BYTES]:
        return None
    return data.decode("utf-8", "ignore")


def rank_files(
    repo_dir: str | Path, issue_text: str, k: int = 10, include_tests: bool = False
) -> list[str]:
    """The ``k`` tracked files most likely to change for ``issue_text``.

    Score = BM25 over file content plus path tokens (weighted ``PATH_WEIGHT``).
    Ties break on path order; files with no matching term are never returned.
    """
    root = Path(repo_dir)
    paths: list[str] = []
    docs: list[list[str]] = []
    for rel in tracked_files(root):
        if not include_tests and is_test_path(rel):
            continue
        text = read_text(root / rel)
        if text is None:
            continue
        paths.append(rel)
        docs.append(tokens(rel) * PATH_WEIGHT + tokens(text))
    scores = BM25Index(docs).scores(tokens(issue_text))
    ranked = sorted(scores, key=lambda i: (-scores[i], paths[i]))
    return [paths[i] for i in ranked[: max(k, 0)]]
