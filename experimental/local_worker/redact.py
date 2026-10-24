"""What of a tool call may enter the public corpus line (#88, Plan v2 point 6; review finding 9).

Paths stay readable up to MAX_PATH_CHARS characters; a longer one becomes ``{"sha256", "bytes"}``
like written text, so no field of a line grows with what the model chose to write. A list of
changed paths keeps at most MAX_CHANGED_PATHS entries; the cut records the full count next to it.
"""

from __future__ import annotations

import hashlib
from typing import Any

TEXT_ARGS = ("content", "target", "replacement")
MAX_PATH_CHARS = 512
MAX_CHANGED_PATHS = 64


def digest(text: Any) -> dict:
    data = str(text).encode("utf-8", errors="surrogatepass")
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def is_digest(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {"sha256", "bytes"}


def public_path(path: str) -> str | dict:
    return path if len(path) <= MAX_PATH_CHARS else digest(path)


def public_paths(paths: list[str]) -> list[str | dict]:
    return [public_path(p) for p in paths[:MAX_CHANGED_PATHS]]


def is_public_path(value: Any) -> bool:
    return (isinstance(value, str) and len(value) <= MAX_PATH_CHARS) or is_digest(value)


def public_args(args: dict) -> dict:
    """Allowlisted tool arguments: path, target_index; written text only as sha256 and size."""
    out: dict[str, Any] = {}
    path = args.get("path")
    if isinstance(path, str):
        out["path"] = public_path(path)
    index = args.get("target_index")
    if isinstance(index, int) and not isinstance(index, bool):
        out["target_index"] = index
    for key in TEXT_ARGS:
        if key in args:
            out[key] = digest(args[key])
    if "status" in args and args["status"] in ("DONE", "NEEDS_SPEC"):
        out["status"] = args["status"]
    return out
