"""Tool layer of the local worker arm (#88): every tool call runs as _cairnwork_worker.

The agent loop (not written yet; it waits for the PAUSE pilot) may run as the Owner, but it must
never read the checkout itself: the only text it may hand the model is ``ToolCall.output``, which is
what the wrapper ``local-worker-tool`` printed while running as the worker account under its own
Seatbelt profile (knowledge addendum 2026-10-23 §0a item 1). This module therefore opens exactly one
file itself, the append-only call log.

Each call is logged as one JSON line carrying the per-turn fields of the episode corpus spec
(EPISODE_CORPUS_SPEC_ZH §4: action_type, tool_args, exit_code, stdout_bytes, stdout_truncated,
error_category, duration_ms); the loop adds the token counts with ``ToolCall.as_turn``.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

WORKER_USER = "_cairnwork_worker"
INSTALLED_WRAPPER = Path("/Users/Shared/cairnwork-worker/bin/local-worker-tool")
SUDO = "/usr/bin/sudo"
REFUSED_EXIT = 64
DEFAULT_MAX_OUTPUT_BYTES = 20_000
DEFAULT_TIMEOUT_SECONDS = 300.0

# (exit code or None on timeout, stdout, stderr)
Invoked = tuple[int | None, bytes, bytes]
Invoker = Callable[[list[str], bytes | None, float], Invoked]


class WorkerIsolationError(RuntimeError):
    """sudo could not start the wrapper as the worker: fail closed, never fall back to the Owner."""


def sudo_argv(wrapper: Path, action: str, run_root: str, args: Sequence[str]) -> list[str]:
    return [SUDO, "-n", "-u", WORKER_USER, str(wrapper), action, run_root, *args]


def direct_argv(wrapper: Path, action: str, run_root: str, args: Sequence[str]) -> list[str]:
    """Owner runs the wrapper itself: Seatbelt only, no uid layer (tests, pre-install canary)."""
    return ["/bin/sh", str(wrapper), action, run_root, *args]


def subprocess_invoker(env: dict[str, str] | None = None) -> Invoker:
    def invoke(argv: list[str], stdin: bytes | None, timeout: float) -> Invoked:
        proc = subprocess.Popen(  # noqa: S603 - argv is built here from fixed parts
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd="/",
        )
        try:
            out, err = proc.communicate(stdin or b"", timeout=timeout)
            return proc.returncode, out, err
        except subprocess.TimeoutExpired:
            proc.terminate()  # sudo relays SIGTERM to the worker command
            try:
                out, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
            return None, out, err

    return invoke


def _clip(data: bytes, limit: int) -> tuple[str, bool]:
    """Head and tail of ``data`` within ``limit`` bytes (test output ends with the summary)."""
    if len(data) <= limit:
        return data.decode("utf-8", errors="replace"), False
    half = limit // 2
    dropped = len(data) - 2 * half
    head = data[:half].decode("utf-8", errors="replace")
    tail = data[-half:].decode("utf-8", errors="replace")
    return f"{head}\n[... {dropped} bytes truncated ...]\n{tail}", True


def error_category(action_type: str, exit_code: int | None, raw: bytes) -> str:
    if exit_code is None:
        return "TIMEOUT"
    if exit_code == 0:
        return "NONE"
    if exit_code == REFUSED_EXIT:
        return "REFUSED"
    if action_type == "run_test":
        if b"SyntaxError" in raw:
            return "SYNTAX_ERROR"
        return {1: "TEST_FAILED", 5: "NO_TESTS"}.get(exit_code, "TOOL_ERROR")
    return "TOOL_ERROR"


@dataclass(frozen=True)
class ToolCall:
    turn_index: int
    action_type: str
    tool_args: dict
    exit_code: int | None
    output: str
    stdout_bytes: int
    stdout_truncated: bool
    error_category: str
    duration_ms: float
    output_sha256: str
    exec_identity: str
    isolation: str
    started_at: str
    raw: bytes = field(default=b"", repr=False, compare=False)

    def record(self) -> dict:
        rec = asdict(self)
        rec.pop("raw")
        rec.pop("output")
        rec["role"] = "tool_result"
        return rec

    def as_turn(self, tokens_prompt: int, tokens_completion: int, tokens_cached: int) -> dict:
        turn = self.record()
        turn.update(
            tokens_prompt=tokens_prompt,
            tokens_completion=tokens_completion,
            tokens_cached=tokens_cached,
        )
        return turn


class WorkerTools:
    """list_dir / read_file / write_file / edit_file / run_tests, each run as the worker.

    ``mode="worker"`` (the only mode an experiment may use) goes through ``sudo -n -u
    _cairnwork_worker`` and the installed wrapper. ``mode="direct"`` runs a wrapper copy as the
    Owner; it exists for tests and for the Seatbelt half of the canary before installation.
    """

    def __init__(
        self,
        run_root: Path,
        log_path: Path,
        *,
        mode: str = "worker",
        wrapper: Path = INSTALLED_WRAPPER,
        invoker: Invoker | None = None,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if mode not in ("worker", "direct"):
            raise ValueError(f"unknown mode {mode!r}")
        self.run_root = str(run_root)
        self.log_path = log_path
        self.mode = mode
        self.wrapper = wrapper
        self.invoker = invoker or subprocess_invoker()
        self.max_output_bytes = max_output_bytes
        self.timeout_seconds = timeout_seconds
        self.identity = WORKER_USER if mode == "worker" else _owner_name()
        self.turn_index = 0

    # -- the actions -------------------------------------------------------------------------
    def list_dir(self, path: str = ".") -> ToolCall:
        return self._call("list_dir", "list", [path], {"path": path})

    def read_file(self, path: str) -> ToolCall:
        return self._call("read_file", "read", [path], {"path": path})

    def write_file(self, path: str, content: str) -> ToolCall:
        args = {"path": path, "content": content}
        return self._call("write_file", "write", [path], args, stdin=content.encode())

    def run_tests(self, pytest_args: Sequence[str]) -> ToolCall:
        args = list(pytest_args)
        return self._call("run_test", "pytest", args, {"pytest_args": args})

    def edit_file(self, path: str, target: str, replacement: str) -> ToolCall:
        """Replace the single exact occurrence of ``target``; the text comes from a worker read."""
        tool_args = {"path": path, "target": target, "replacement": replacement}
        started = time.monotonic()
        rc, out, err = self._invoke("read", [path], None)
        if rc != 0:
            return self._log("edit_file", tool_args, rc, out + err, started)
        try:
            text = out.decode("utf-8")
        except UnicodeDecodeError:
            note = f"edit_file: {path} is not UTF-8 text; nothing written\n"
            return self._log("edit_file", tool_args, 1, note.encode(), started)
        count = text.count(target) if target else 0
        if count != 1:
            note = f"edit_file: target occurs {count} times in {path}; nothing written\n"
            return self._log("edit_file", tool_args, 1, note.encode(), started)
        new = text.replace(target, replacement, 1).encode()
        rc, out, err = self._invoke("write", [path], new)
        return self._log("edit_file", tool_args, rc, out + err, started)

    def sweep(self) -> ToolCall:
        return self._call("sweep", "sweep", [], {})

    # -- plumbing ----------------------------------------------------------------------------
    def _argv(self, action: str, args: Sequence[str]) -> list[str]:
        build = sudo_argv if self.mode == "worker" else direct_argv
        return build(self.wrapper, action, self.run_root, args)

    def _invoke(self, action: str, args: Sequence[str], stdin: bytes | None) -> Invoked:
        rc, out, err = self.invoker(self._argv(action, args), stdin, self.timeout_seconds)
        if self.mode == "worker" and rc not in (0, None) and err.startswith(b"sudo:"):
            raise WorkerIsolationError(err.decode("utf-8", errors="replace").strip())
        return rc, out, err

    def _call(
        self,
        action_type: str,
        action: str,
        args: Sequence[str],
        tool_args: dict,
        stdin: bytes | None = None,
    ) -> ToolCall:
        started = time.monotonic()
        rc, out, err = self._invoke(action, args, stdin)
        call = self._log(action_type, tool_args, rc, out + err, started)
        if rc is None and action != "sweep":
            self.sweep()  # a timed-out command may leave worker processes behind
        return call

    def _log(
        self, action_type: str, tool_args: dict, rc: int | None, raw: bytes, started: float
    ) -> ToolCall:
        output, truncated = _clip(raw, self.max_output_bytes)
        call = ToolCall(
            turn_index=self.turn_index,
            action_type=action_type,
            tool_args=tool_args,
            exit_code=rc,
            output=output,
            stdout_bytes=len(raw),
            stdout_truncated=truncated,
            error_category=error_category(action_type, rc, raw),
            duration_ms=round((time.monotonic() - started) * 1000, 3),
            output_sha256=hashlib.sha256(raw).hexdigest(),
            exec_identity=self.identity,
            isolation="sudo+seatbelt" if self.mode == "worker" else "seatbelt-only",
            started_at=datetime.now(UTC).isoformat(),
            raw=raw,
        )
        self.turn_index += 1
        line = json.dumps(call.record(), ensure_ascii=False, sort_keys=True) + "\n"
        with self.log_path.open("a", encoding="utf-8") as log:  # the one file this module opens
            log.write(line)
            log.flush()
            os.fsync(log.fileno())
        return call


def _owner_name() -> str:
    import pwd

    return pwd.getpwuid(os.getuid()).pw_name
