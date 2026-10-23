"""Read a commit's tracked text files from a trusted repository's object store.

The finder answers a worker's request from Cairnwork's own repository at the task's base
commit, never from the worker's clone: git never runs in a worker-controlled directory (a
planted ``.git/config`` could otherwise run a command through ``core.fsmonitor`` and the
like). The same no-hooks / no-fsmonitor options as ``cairnwork.gitcmd`` are applied, and no
``GIT_*`` variable is inherited. Nothing is checked out and nothing is written.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

from .retrieval import MAX_FILE_BYTES

_SNIFF_BYTES = 8192
_TIMEOUT_SECONDS = 120
_SAFE = (
    "--no-replace-objects",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.untrackedCache=false",
)


class CommitSourceError(Exception):
    pass


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        done = subprocess.run(  # noqa: S603 - fixed git argv, no shell
            ["git", *_SAFE, "-C", str(repo), *args],  # noqa: S607
            input=stdin,
            capture_output=True,
            check=True,
            timeout=_TIMEOUT_SECONDS,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CommitSourceError(f"git {args[0]} failed") from exc
    return done.stdout


def _blobs(repo: Path, commit: str) -> list[tuple[str, str]]:
    """``(path, oid)`` of regular-file blobs no larger than MAX_FILE_BYTES, path order."""
    out = _git(repo, "ls-tree", "-r", "-l", "-z", "--full-tree", commit)
    blobs = []
    for entry in out.split(b"\0"):
        if not entry:
            continue
        meta, _, raw_path = entry.partition(b"\t")
        mode, kind, oid, size = meta.split()
        if kind != b"blob" or mode not in (b"100644", b"100755"):
            continue  # symlinks and submodules are never read
        if int(size) > MAX_FILE_BYTES:
            continue
        blobs.append((raw_path.decode("utf-8", "surrogateescape"), oid.decode()))
    return sorted(blobs)


def commit_texts(repo: str | Path, commit: str) -> Iterator[tuple[str, str]]:
    """``(path, text)`` for each tracked text file of ``commit`` in ``repo``.

    Binary files (a NUL in the first 8 KiB), symlinks, submodules and files over
    MAX_FILE_BYTES are skipped, exactly as for a working tree.
    """
    root = Path(repo)
    resolved = _git(root, "rev-parse", "--verify", "--end-of-options", f"{commit}^{{commit}}")
    blobs = _blobs(root, resolved.decode().strip())
    if not blobs:
        return
    stream = _git(root, "cat-file", "--batch", stdin=b"".join(f"{o}\n".encode() for _, o in blobs))
    pos = 0
    for path, oid in blobs:
        end = stream.index(b"\n", pos)
        header = stream[pos:end].split()
        if len(header) != 3 or header[0].decode() != oid or header[1] != b"blob":
            raise CommitSourceError("unexpected cat-file output")
        size = int(header[2])
        data = stream[end + 1 : end + 1 + size]
        pos = end + 1 + size + 1  # the blob is followed by one LF
        if b"\0" not in data[:_SNIFF_BYTES]:
            yield path, data.decode("utf-8", "ignore")
