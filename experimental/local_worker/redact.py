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


PUBLIC_ARGS = {  # per action: the only argument keys a line may carry
    "list_dir": ("path",),
    "read_file": ("path",),
    "write_file": ("path", "content"),
    "edit_file": ("path", "target", "replacement"),
    "run_test": ("target_index",),
    "finish": ("status",),
}


def public_args(action_type: str, args: dict, exit_code: int | None) -> dict:
    """Allowlisted tool arguments of one turn (Codex review of #239, B1).

    Only the keys PUBLIC_ARGS lists for the action. A path stays readable only when the call
    succeeded (exit 0), so it named a real file or directory of the checkout, and it is at most
    MAX_PATH_CHARS long; any other path is model text and becomes a digest, like written text.
    """
    out: dict[str, Any] = {}
    for key in PUBLIC_ARGS.get(action_type, ()):
        if key not in args:
            continue
        value = args[key]
        if key == "path":
            real = isinstance(value, str) and exit_code == 0
            out[key] = public_path(value) if real else digest(value)
        elif key in TEXT_ARGS:
            out[key] = digest(value)
        elif key == "target_index" and isinstance(value, int) and not isinstance(value, bool):
            out[key] = value
        elif key == "status" and value in ("DONE", "NEEDS_SPEC"):
            out[key] = value
    return out
