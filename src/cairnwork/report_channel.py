"""Parent side of the descriptor hand-over to the report-guard plugin (see report_guard.py).

``open_channel`` makes the two descriptors the child inherits through ``pass_fds``: a pipe that
already holds the nonce (write end closed, so the plugin reads to EOF) and an anonymous file the
plugin writes its record into. Neither has a name in the filesystem and neither number is secret
on its own; what the child learns from the environment is only ``CAIRNWORK_GUARD_FDS``, which
the plugin removes at import. The parent reads the record from its own descriptor.

Higher hurdle, not a boundary: code in the pytest process can still scan its open descriptors.
"""

from __future__ import annotations

import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from cairnwork import report_guard


@dataclass
class GuardChannel:
    nonce: str
    _report: IO[bytes]
    _nonce_read: int  # the parent's copy of the pipe's read end; -1 once closed

    @property
    def pass_fds(self) -> tuple[int, int]:
        return (self._nonce_read, self._report.fileno())

    def env(self) -> dict[str, str]:
        nonce_fd, report_fd = self.pass_fds
        return {report_guard.FDS_ENV: f"{nonce_fd},{report_fd}"}

    def read(self) -> report_guard.Verdict:
        return report_guard.read_verdict_fd(self._report.fileno(), self.nonce)

    def close(self) -> None:
        if self._nonce_read >= 0:
            os.close(self._nonce_read)
            self._nonce_read = -1
        self._report.close()


def open_channel(private: Path) -> GuardChannel:
    """A fresh channel with a new nonce; ``private`` holds the (immediately unlinked) report."""
    nonce = secrets.token_hex(16)
    read_end, write_end = os.pipe()
    try:
        os.write(write_end, nonce.encode())  # 32 bytes: far below the pipe buffer
    except BaseException:
        os.close(read_end)
        raise
    finally:
        os.close(write_end)
    try:
        report = tempfile.TemporaryFile(dir=private)  # noqa: SIM115 - closed by GuardChannel.close
    except BaseException:
        os.close(read_end)
        raise
    return GuardChannel(nonce, report, read_end)
