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
                "--tmpfs",
                "/tmp",
                "--size",
                "67108864",
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
    flags = ["--tmpfs", "/dev/shm"]
    if supports_tmpfs_size(binary):
        flags += ["--size", "67108864"]
    return flags
