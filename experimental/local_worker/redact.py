"""What of a tool call may enter the public corpus line (#88, Plan v2 point 6; review finding 9).

A path name the model chose is model text (Claude review of #239, NB4): a successful write_file
creates whatever name it was given. A path is therefore published readable only when it existed at
base, i.e. it is one of the base commit's paths (the worker's ``tracked`` query: every file and
directory of HEAD, plus "."), and is at most MAX_PATH_CHARS characters long. Any other path (a new
one, one the run never confirmed, or any path when the base listing was not observed) becomes
``{"sha256", "bytes", "ext"}``: a digest, plus its extension only when that is one of the fixed
PATH_EXTS (so the extension carries no free text). The same rule covers tool arguments,
first_edit_cut.target_file, its changed_paths and first_source_edit.path. Written text (content,
target, replacement) is ``{"sha256", "bytes"}``. A list of changed paths keeps at most
MAX_CHANGED_PATHS entries; the cut records the full count next to it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection
from typing import Any

TEXT_ARGS = ("content", "target", "replacement")
MAX_PATH_CHARS = 512
MAX_CHANGED_PATHS = 64
PATH_EXTS = (".py", ".pyi", ".pyx", ".pxd", ".c", ".h", ".cc", ".cpp", ".rs", ".go", ".js", ".ts")
PATH_EXTS += (".json", ".toml", ".cfg", ".ini", ".yaml", ".yml", ".txt", ".md", ".rst", ".csv")
PATH_EXTS += (".sh", ".log", ".pyc", ".lock", ".xml", ".html", ".css", ".sql", ".in", ".gitignore")

BasePaths = Collection[str] | None  # the base's paths, or None when they were not observed


def digest(text: Any) -> dict:
    data = str(text).encode("utf-8", errors="surrogatepass")
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def _digest_ok(value: dict) -> bool:
    sha, size = value["sha256"], value["bytes"]
    hex_ok = isinstance(sha, str) and len(sha) == 64 and set(sha) <= set("0123456789abcdef")
    return hex_ok and isinstance(size, int) and not isinstance(size, bool) and size >= 0


def is_digest(value: Any) -> bool:
    """Exactly {sha256: 64 lowercase hex characters, bytes: a non-negative integer}."""
    return isinstance(value, dict) and set(value) == {"sha256", "bytes"} and _digest_ok(value)


def path_ext(path: str) -> str | None:
    """The name's extension, lowercased, if it is one of PATH_EXTS; else None."""
    name = path.rsplit("/", 1)[-1].lower()
    dot = name.rfind(".")
    ext = name[dot:] if dot >= 0 else ""
    return ext if ext in PATH_EXTS else None


def path_digest(path: Any) -> dict:
    """A path that may not be published readable: its digest and allowlisted extension."""
    return {**digest(path), "ext": path_ext(path) if isinstance(path, str) else None}


def is_path_digest(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"sha256", "bytes", "ext"}:
        return False
    return _digest_ok(value) and (value["ext"] is None or value["ext"] in PATH_EXTS)


def public_path(path: str, base_paths: BasePaths) -> str | dict:
    """``path`` itself if it existed at base and is short enough, else its path_digest."""
    known = base_paths is not None and path in base_paths
    return path if known and len(path) <= MAX_PATH_CHARS else path_digest(path)


def public_paths(paths: list[str], base_paths: BasePaths) -> list[str | dict]:
    return [public_path(p, base_paths) for p in paths[:MAX_CHANGED_PATHS]]


def is_public_path(value: Any, plain_ok: bool = True) -> bool:
    """A path_digest, or (when ``plain_ok``) a readable path of at most MAX_PATH_CHARS."""
    plain = plain_ok and isinstance(value, str) and len(value) <= MAX_PATH_CHARS
    return plain or is_path_digest(value)


PUBLIC_ARGS = {  # per action: the only argument keys a line may carry
    "list_dir": ("path",),
    "read_file": ("path",),
    "write_file": ("path", "content"),
    "edit_file": ("path", "target", "replacement"),
    "run_test": ("target_index",),
    "finish": ("status",),
}


def public_args(
    action_type: str, args: dict, exit_code: int | None, base_paths: BasePaths = None
) -> dict:
    """Allowlisted tool arguments of one turn (Codex review of #239, B1; Claude review, NB4).

    Only the keys PUBLIC_ARGS lists for the action. A path stays readable only when the call
    succeeded (exit 0) and the path existed at base (public_path); any other path is model text
    and becomes a path_digest. Written text becomes a digest."""
    out: dict[str, Any] = {}
    for key in PUBLIC_ARGS.get(action_type, ()):
        if key not in args:
            continue
        value = args[key]
        if key == "path":
            real = isinstance(value, str) and exit_code == 0
            out[key] = public_path(value, base_paths) if real else path_digest(value)
        elif key in TEXT_ARGS:
            out[key] = digest(value)
        elif key == "target_index" and isinstance(value, int) and not isinstance(value, bool):
            out[key] = value
        elif key == "status" and value in ("DONE", "NEEDS_SPEC"):
            out[key] = value
    return out
