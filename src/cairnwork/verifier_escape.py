"""Tell a harmless launchd system daemon from a process the candidate left behind (cairnwork#37).

On a host with a full Xcode, running /usr/bin/git goes through xcrun/xcodebuild, and launchd starts
the account's own distnoted (ppid 1). It is harmless and killed with everything else, but
VerifierAccount must not count it as an escape. A process is exempt only by its REAL executable path
(proc_pidpath, never the forgeable argv[0]/ps-comm), its parent being launchd (ppid 1), and an Apple
code signature — so a candidate that renames itself to a daemon, or re-parents to launchd while
running another binary, keeps its own real path and is still counted.
"""

from __future__ import annotations

import subprocess

SYSTEM_DAEMONS = frozenset({"/usr/sbin/distnoted"})
# Printed, as the account, one "pid\treal-path" line per queried pid. proc_pidpath returns the
# running image's vnode path from the kernel, which the target process cannot forge.
PIDPATH_HELPER = (
    "import ctypes,sys\n"
    "libc=ctypes.CDLL(None)\n"
    "libc.proc_pidpath.restype=ctypes.c_int\n"
    "b=ctypes.create_string_buffer(4096)\n"
    "for a in sys.argv[1:]:\n"
    "    n=libc.proc_pidpath(int(a),b,4096)\n"
    "    if n>0: sys.stdout.write('%s\\t%s\\n'%(a,b.value[:n].decode('utf-8','replace')))\n"
)


def apple_signed(path: str) -> bool:
    """True only if ``path`` is a genuine Apple-OS binary (the 'anchor apple' requirement, which a
    third-party Developer ID signature does not satisfy). Reads the file on disk, as the owner."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, path from proc_pidpath
            ["/usr/bin/codesign", "--verify", "-R=anchor apple", "--", path],
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def is_exempt(real_path: str | None, ppid: int) -> bool:
    """Whether a process is a launchd system daemon that must not count as an escape: its real
    executable path is allow-listed, its parent is launchd (ppid 1), and it is Apple-signed."""
    return ppid == 1 and real_path in SYSTEM_DAEMONS and apple_signed(real_path)
