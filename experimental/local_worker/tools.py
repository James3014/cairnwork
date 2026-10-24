"""Tool layer of the local worker arm (#88): every tool call runs as _cairnwork_worker.

The agent loop (agent.py) may run as the Owner, but it must
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
import secrets
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

WORKER_USER = "_cairnwork_worker"
INSTALLED_WRAPPER = Path("/Users/Shared/cairnwork-worker/local-bin/local-worker-tool")
SUDO = "/usr/bin/sudo"
REFUSED_EXIT = 64
TERM_GRACE_SECONDS = 5.0  # after SIGTERM, and again after SIGKILL, before giving up on a wait
KILL_GRACE_SECONDS = 2 * TERM_GRACE_SECONDS  # how long a timed-out call can outlive its timeout
SWEEP_TIMEOUT_SECONDS = 10.0  # the sweep after a pytest run or a timed-out tool call
TOOL_ACTIONS = ("list", "read", "write", "pytest")  # wrapper actions the model's tools use
START_MARKER = "LOCAL_WORKER_STARTED"  # first line the wrapper prints on stderr: "<marker> <nonce>"
DEFAULT_MAX_OUTPUT_BYTES = 20_000
DEFAULT_TIMEOUT_SECONDS = 300.0
CAPTURE_LIMIT = 8 * 1024 * 1024  # per stream; output goes to a temp file, never unbounded memory

# (exit code or None on timeout, stdout, stderr). A stream longer than CAPTURE_LIMIT is a capped
# capture (subprocess_invoker returns head + marker + tail, always more than CAPTURE_LIMIT bytes;
# a stream it read whole is at most CAPTURE_LIMIT): ToolCall.capture_complete is then False.
Invoked = tuple[int | None, bytes, bytes]
Invoker = Callable[[list[str], bytes | None, float], Invoked]


class WorkerIsolationError(RuntimeError):
    """sudo could not start the wrapper as the worker: fail closed, never fall back to the Owner."""


class ToolDeadline(TimeoutError):
    """Not enough time before ``WorkerTools.deadline`` to start a wrapper call and wait it out
    (its timeout, the kill grace and, for a tool action, the sweep after it): nothing started."""


def _nonce_args(nonce: str | None) -> list[str]:
    return ["--start-nonce", nonce] if nonce else []


def sudo_argv(
    wrapper: Path, action: str, run_root: str, args: Sequence[str], nonce: str | None = None
) -> list[str]:
    head = [SUDO, "-n", "-u", WORKER_USER, str(wrapper)]
    return [*head, *_nonce_args(nonce), action, run_root, *args]


def direct_argv(
    wrapper: Path, action: str, run_root: str, args: Sequence[str], nonce: str | None = None
) -> list[str]:
    """Owner runs the wrapper itself: Seatbelt only, no uid layer (tests, pre-install canary)."""
    return ["/bin/sh", str(wrapper), *_nonce_args(nonce), action, run_root, *args]


def _captured(fh) -> bytes:
    """The whole capture file if it has at most CAPTURE_LIMIT bytes, else its head and tail
    around a marker (more than CAPTURE_LIMIT bytes in all: that length says it was capped)."""
    size = os.fstat(fh.fileno()).st_size
    fh.seek(0)
    if size <= CAPTURE_LIMIT:
        return fh.read()
    half = CAPTURE_LIMIT // 2
    head = fh.read(half)
    fh.seek(size - half)
    return head + f"\n[... {size - 2 * half} bytes not captured ...]\n".encode() + fh.read(half)


def _signal(proc: subprocess.Popen, kill: bool) -> None:
    try:
        proc.kill() if kill else proc.terminate()
    except (PermissionError, ProcessLookupError):
        pass  # setuid sudo may refuse the Owner's signal; the caller sweeps the run instead


def subprocess_invoker(env: dict[str, str] | None = None) -> Invoker:
    """Run argv with stdout/stderr in temp files (bounded memory, and no hang on a grandchild
    that keeps a pipe open). On timeout: signal, wait briefly, and report exit code None."""

    def invoke(argv: list[str], stdin: bytes | None, timeout: float) -> Invoked:
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(  # noqa: S603 - argv is built here from fixed parts
                argv, stdin=subprocess.PIPE, stdout=out, stderr=err, env=env, cwd="/"
            )
            rc: int | None
            try:
                proc.communicate(stdin or b"", timeout=timeout)
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                rc = None
                _signal(proc, kill=False)
                try:
                    proc.wait(timeout=TERM_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    _signal(proc, kill=True)
                    try:
                        proc.wait(timeout=TERM_GRACE_SECONDS)  # reap it: no zombie per timeout
                    except subprocess.TimeoutExpired:
                        pass  # a refused signal; the sweep that follows kills the worker side
            return rc, _captured(out), _captured(err)

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
    # False when stdout or stderr was capped (tools.CAPTURE_LIMIT): ``raw`` is then not the whole
    # output, and a git query's result is not observed (Claude review of #239, NB5).
    capture_complete: bool = True

    def record(self) -> dict:
        rec = asdict(self)
        rec.pop("raw")
        rec.pop("output")
        rec["role"] = "tool_result"
        return rec

    def as_turn(
        self, tokens_prompt: int | None, tokens_completion: int | None, tokens_cached: int | None
    ) -> dict:
        turn = self.record()
        turn.update(
            tokens_prompt=tokens_prompt,
            tokens_completion=tokens_completion,
            tokens_cached=tokens_cached,
        )
        return turn


class WorkerTools:
    """list_dir / read_file / write_file / edit_file / run_tests / status / numstat / tracked, as
    the worker.

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
        # Absolute end (on ``clock``) for every wrapper call, the waits after a timeout and the
        # sweep included; None: each call just gets timeout_seconds (canary, tests).
        self.deadline: float | None = None
        self.clock: Callable[[], float] = time.monotonic
        self.calls_started = 0  # wrapper processes started: a tool may have changed the tree
        self.last_complete = True  # whether the last wrapper call's output was captured whole

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
        budget_end = self.clock() + self.timeout_seconds  # one timeout for the read and the write
        rc, out, err = self._invoke("read", [path], None)
        if rc != 0:
            call = self._log("edit_file", tool_args, rc, out + err, started)
            self._after("read", rc)
            return call
        if not self.last_complete:  # a capped read is not the file: never write it back
            note = f"edit_file: {path} is too large to edit; nothing written\n"
            return self._log("edit_file", tool_args, 1, note.encode(), started)
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
        left = budget_end - self.clock()
        if left <= 0:
            return self._log("edit_file", tool_args, None, b"edit_file: no time left\n", started)
        rc, out, err = self._invoke("write", [path], new, cap=left)
        call = self._log("edit_file", tool_args, rc, out + err, started)
        self._after("write", rc)
        return call

    def sweep(self) -> ToolCall:
        return self._call("sweep", "sweep", [], {})

    def status(self) -> ToolCall:
        """Fixed ``git status --porcelain=v2 --branch -z -uall`` of the checkout, then its empty
        directories, FIFOs and sockets (gitstate.parse_status), as the worker."""
        return self._call("status", "status", [], {})

    def tracked(self) -> ToolCall:
        """Fixed ``git ls-tree -r -t -z --name-only HEAD``: the base's paths, as the worker."""
        return self._call("tracked", "tracked", [], {})

    def numstat(self) -> ToolCall:
        """``PATCH <added> <removed> <binary_files_changed>`` against HEAD, untracked and ignored
        files included, counted from the ``--text -U0`` patch (gitstate.parse_numstat)."""
        return self._call("numstat", "numstat", [], {})

    # -- plumbing ----------------------------------------------------------------------------
    def _argv(self, action: str, args: Sequence[str], nonce: str | None = None) -> list[str]:
        build = sudo_argv if self.mode == "worker" else direct_argv
        return build(self.wrapper, action, self.run_root, args, nonce)

    def _timeout(self, action: str, cap: float | None) -> float:
        """The timeout of the next wrapper call: at most timeout_seconds and ``cap``, and short
        enough that the call, its kill grace and (for a tool action) the sweep after it with its
        own grace all end by ``deadline``. Raises ToolDeadline when that leaves nothing."""
        timeout = min(self.timeout_seconds, cap if cap is not None else self.timeout_seconds)
        if self.deadline is None:
            return timeout
        left = self.deadline - self.clock() - KILL_GRACE_SECONDS
        if action in TOOL_ACTIONS:
            left -= SWEEP_TIMEOUT_SECONDS + KILL_GRACE_SECONDS
        if left <= 0 or timeout <= 0:
            raise ToolDeadline(f"no time left before the deadline for {action}")
        return min(timeout, left)

    def _invoke(
        self, action: str, args: Sequence[str], stdin: bytes | None, cap: float | None = None
    ) -> Invoked:
        # The wrapper prints "<START_MARKER> <nonce>" as the first thing on stderr. Whether it
        # started is decided by that line alone, never by the exit code or by text such as
        # "sudo:" in the output: a test the model wrote may exit 126/127 or print anything.
        timeout = self._timeout(action, cap)
        nonce = secrets.token_hex(8)
        argv = self._argv(action, args, nonce)
        self.calls_started += 1
        rc, out, err = self.invoker(argv, stdin, timeout)
        self.last_complete = len(out) <= CAPTURE_LIMIT and len(err) <= CAPTURE_LIMIT
        marker = f"{START_MARKER} {nonce}\n".encode()
        if not err.startswith(marker):
            # No rule, missing binary, refused sudo: the wrapper never ran. Fail closed.
            detail = err[:4096].decode("utf-8", errors="replace").strip()
            if action != "sweep":
                self._sweep_quietly()  # a run that did start may have left processes behind
            raise WorkerIsolationError(f"wrapper did not start (exit {rc}): {detail}")
        return rc, out, err[len(marker) :]

    def _sweep_quietly(self) -> None:
        try:
            self._call("sweep", "sweep", [], {}, cap=SWEEP_TIMEOUT_SECONDS)
        except (WorkerIsolationError, ToolDeadline):
            pass  # the loop's cleanup sweeps again

    def _call(
        self,
        action_type: str,
        action: str,
        args: Sequence[str],
        tool_args: dict,
        stdin: bytes | None = None,
        cap: float | None = None,
    ) -> ToolCall:
        started = time.monotonic()
        rc, out, err = self._invoke(action, args, stdin, cap)
        call = self._log(action_type, tool_args, rc, out + err, started, self.last_complete)
        self._after(action, rc)
        return call

    def _after(self, action: str, rc: int | None) -> None:
        # Test code can leave processes behind (and a timed-out command certainly may): sweep
        # after every pytest run and every timeout. The sweep finds a process by its run tag or
        # its cwd; one that cleared its environment and left the run root survives it and can go
        # on changing the checkout (wrapper: sweep_pids). It cannot falsify what status and
        # numstat report: they write only in <run_root>/q, which its profile can neither read nor
        # write (Claude review of #239, NB1), so they report the tree as it is when they run.
        # The time for this sweep was kept free when the tool call's timeout was chosen.
        if action in TOOL_ACTIONS and (action == "pytest" or rc is None):
            self._call("sweep", "sweep", [], {}, cap=SWEEP_TIMEOUT_SECONDS)

    def _log(
        self,
        action_type: str,
        tool_args: dict,
        rc: int | None,
        raw: bytes,
        started: float,
        complete: bool = True,
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
            capture_complete=complete,
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
