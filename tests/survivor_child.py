"""A worker child that outlives its run (crash_driver.py 'survivor' mode, #244 window 1).

Usage: python survivor_child.py <state dir>
Writes its pid to <state>/survivor.pid, then waits for <state>/new-attempt-running. Only if that
file appears does it copy the token inside it to <state>/old-attempt-wrote, so the write depends
causally on the new attempt's worker; with no signal it exits without writing. Both sides publish
their file by rename, so neither can read it empty.
"""

import os
import sys
import time
from pathlib import Path


def publish(path: Path, text: str) -> None:
    """Write then rename: a reader never sees the file empty or half written."""
    part = path.with_name(path.name + ".part")
    part.write_text(text)
    os.replace(part, path)


state = Path(sys.argv[1])
publish(state / "survivor.pid", str(os.getpid()))
go = state / "new-attempt-running"
deadline = time.monotonic() + 120
while not go.exists():
    if time.monotonic() > deadline:
        sys.exit(1)
    time.sleep(0.05)
publish(state / "old-attempt-wrote", go.read_text())
time.sleep(120)
