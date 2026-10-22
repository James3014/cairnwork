from __future__ import annotations

import subprocess
from functools import lru_cache


@lru_cache(maxsize=1)
def supports_tmpfs_size(binary: str) -> bool:
    """True if the installed bwrap supports the --size option on tmpfs."""
    if not binary:
        return False
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv
            [
                binary,
                "--unshare-user",
                "--ro-bind",
                "/",
                "/",
                # bwrap applies --size to the *next* --tmpfs, so it must come first; a trailing
                # --size is accepted by some versions and would make this probe lie.
                "--size",
                "67108864",
                "--tmpfs",
                "/tmp",
                "true",
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def shm_flags(binary: str) -> list[str]:
    """Bubblewrap flags to mount a capped /dev/shm, or [] if --size is unsupported."""
    if not supports_tmpfs_size(binary):
        return []
    return ["--size", "67108864", "--tmpfs", "/dev/shm"]


def shm_unavailable_reason(binary: str) -> str | None:
    """Reason why the /dev/shm mount was withheld, or None if available."""
    if not supports_tmpfs_size(binary):
        return "BWRAP_TMPFS_SIZE_UNSUPPORTED"
    return None
