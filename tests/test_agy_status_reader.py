"""Agy worker's status reader and stuck-reader detection (split from test_agy_worker.py)."""

import io
import threading
import time

from cairnwork.workers.agy import _stop
from cairnwork.workers.agy_dispatch import StatusReader


def test_a_failing_stderr_read_keeps_draining_and_marks_the_status_unknown():
    class Flaky(io.BytesIO):
        failed = False

        def readline(self, size: int | None = -1):
            if not self.failed:
                self.failed = True
                raise OSError("boom")
            return super().readline(size)

    stream = Flaky(b'NEXUS_AGY_DISPATCH {"status": "pool_exhausted"}\n')
    reader = StatusReader(stream)
    reader.run()
    assert reader.unknown and "unknown" in reader.describe()
    assert reader.status() == {"status": "pool_exhausted"}  # it kept draining


def test_a_reader_that_is_still_alive_counts_as_holding_the_output(tmp_path):
    class P:
        pid = 2**22 + 12345  # no such group

        def wait(self):
            pass

    copier = threading.Thread(target=lambda: None)
    copier.start()
    copier.join()
    stuck = threading.Thread(target=time.sleep, args=(3,), daemon=True)
    stuck.start()
    stuck.join = lambda timeout=None: None  # type: ignore[method-assign]
    assert _stop(P(), copier, "attempt-x", stuck) is True
