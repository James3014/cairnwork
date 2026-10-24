"""Minimal local agent loop for the local worker arm (#88, Plan v2 on the Issue).

The loop runs as the Owner and talks to one loopback chat endpoint (endpoint.py). It never opens a
file: every byte it hands the model is ``ToolCall.output`` from the worker tools (tools.py, each
call ``sudo -n -u _cairnwork_worker`` + Seatbelt), and the only file I/O of a run is corpus_io.py
(task.json, the canary report, the output directory). After every tool call the worker runs the
fixed ``status`` query; the observed tree decides the cut (episode.first_edit_cut).

Limits (frozen in the manifest): ``max_steps`` turns (one model reply plus at most one tool call;
extra tool calls in a reply are dropped and counted), one deadline for the whole episode
(``max_wall_seconds`` minus a cleanup reserve), and a context cap on the last reply's prompt +
completion tokens. Every endpoint request gets the deadline itself. A wrapper call that times out
can outlive its timeout by up to tools.KILL_GRACE_SECONDS (SIGTERM, then SIGKILL), so a tool call
(edit_file makes two wrapper calls under one timeout) gets min(tool cap, time left minus twice that
grace), and the cleanup reserve is split into three equal steps (the status query after a turn,
sweep, numstat), each with room for its own grace: the episode ends within max_wall_seconds.

``run_tests`` takes no free-form arguments: it runs ``pytest -q --tb=short -- <targets>`` over the
task's declared public test targets, or the one the model picks by ``target_index``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from experimental.local_worker.agent_protocol import (
    NUDGE,
    RUN_TESTS_ARGS,
    SYSTEM_PROMPT,
    TOOL_SCHEMAS,
    BadCall,
    decode_call,
    render,
)
from experimental.local_worker.endpoint import (
    Endpoint,
    EndpointError,
    EndpointTimeout,
    parse_usage,
)
from experimental.local_worker.episode import LoopOutcome, TurnLog
from experimental.local_worker.gitstate import TreeState, parse_numstat
from experimental.local_worker.gitstate import parse_status as _parse_status
from experimental.local_worker.tools import (
    KILL_GRACE_SECONDS,
    ToolCall,
    WorkerIsolationError,
    WorkerTools,
)

UTC8 = timezone(timedelta(hours=8))  # spec §2: timestamps in UTC+8


@dataclass(frozen=True)
class Limits:
    context_cap: int
    max_steps: int = 40
    max_wall_seconds: float = 1800.0
    cut_seconds: float = 900.0
    tool_cap_seconds: float = 300.0
    cleanup_reserve_seconds: float = 60.0
    max_completion_tokens: int = 4096

    def __post_init__(self) -> None:
        if self.step_seconds < 1.0:
            raise ValueError(f"cleanup_reserve_seconds must be >= {3 * (KILL_GRACE_SECONDS + 1)}")

    @property
    def step_seconds(self) -> float:
        """Timeout of each bookkeeping step (status, sweep, numstat): a third of the reserve,
        less the kill grace a timed-out step may still use."""
        return self.cleanup_reserve_seconds / 3 - KILL_GRACE_SECONDS


@dataclass(frozen=True)
class Task:
    task_id: str
    repo: str
    prompt: str
    public_tests: tuple[str, ...]


@dataclass(frozen=True)
class Settings:
    model: str
    limits: Limits
    seed: int | None = None
    temperature: float = 0.0


class RefusedStart(RuntimeError):
    """The checkout is not clean at the expected base: no episode is run."""


class _StatusUnavailable(RuntimeError):
    pass


def initial_messages(task: Task) -> list[dict]:
    targets = "\n".join(f"{i}: {t}" for i, t in enumerate(task.public_tests))
    user = f"{task.prompt}\n\nPublic test targets (run_tests target_index):\n{targets}"
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


class AgentLoop:
    def __init__(
        self,
        tools: WorkerTools,
        endpoint: Endpoint,
        task: Task,
        settings: Settings,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.tools, self.endpoint, self.task, self.settings = tools, endpoint, task, settings
        self.limits = settings.limits
        self.clock, self.now = clock, now
        self.abort_detail: str | None = None
        self._start = 0.0
        self._offset = 0.0

    def _elapsed(self) -> float:
        """Seconds on the logical episode clock (a resume starts at the parent's pre-cut time):
        what the limits use."""
        return self.clock() - self._start

    def _own(self) -> float:
        """Seconds this run itself has spent: what every recorded time uses (TurnLog t_*)."""
        return self._elapsed() - self._offset

    def observe(self, timeout: float) -> TreeState:
        self.tools.timeout_seconds = timeout
        call = self.tools.status()
        return _parse_status(call.raw) if call.exit_code == 0 else TreeState(observed=False)

    def run(
        self,
        messages: list[dict],
        *,
        first_turn: int = 0,
        elapsed_offset: float = 0.0,
        expected_base: str | None = None,
    ) -> LoopOutcome:
        """Run from ``messages`` (a fresh start or a resume prefix) until a stop; never raises
        once the base is verified: every stop is recorded in the returned outcome."""
        lim = self.limits
        self._start = self.clock() - elapsed_offset
        self._offset = elapsed_offset
        deadline = self._start + lim.max_wall_seconds - lim.cleanup_reserve_seconds
        base = self.observe(lim.step_seconds)
        if not base.observed or base.entries or base.head is None:
            raise RefusedStart("checkout is not a clean git tree at its base")
        if expected_base is not None and base.head != expected_base:
            raise RefusedStart(f"checkout HEAD {base.head} is not the pinned base {expected_base}")
        out = LoopOutcome(base=base, started_at=self.now().astimezone(UTC8).isoformat())
        try:
            self._loop(messages, out, first_turn, deadline)
        except (EndpointTimeout, TimeoutError):
            out.exit_reason, out.timed_out, out.limit_hit = "TIMEOUT", True, "max_wall_seconds"
        except KeyboardInterrupt:
            out.exit_reason, out.interrupted, out.abort_cause = "ERROR_ABORT", True, "interrupted"
        except EndpointError as exc:
            self._abort(out, "endpoint_error", exc)
        except WorkerIsolationError as exc:
            self._abort(out, "worker_isolation", exc)
        except _StatusUnavailable as exc:
            self._abort(out, "status_unavailable", exc)
        self._cleanup(out)
        out.ended_at = self.now().astimezone(UTC8).isoformat()
        out.wall_seconds = self._own()
        return out

    def _abort(self, out: LoopOutcome, cause: str, exc: Exception) -> None:
        out.exit_reason, out.abort_cause = "ERROR_ABORT", cause
        self.abort_detail = f"{type(exc).__name__}: {exc}"

    def _loop(self, messages: list[dict], out: LoopOutcome, turn: int, deadline: float) -> None:
        while True:
            stop = self._limit(turn, messages, out.turns[-1] if out.turns else None, deadline)
            if stop:
                out.exit_reason, out.limit_hit = stop
                out.timed_out = stop[0] == "TIMEOUT"
                return
            log = TurnLog(index=turn, t_start=self._own())
            try:
                finished = self._step(log, messages, deadline)
            finally:  # a turn counts once the model replied, also when its tool call then failed
                log.t_end = self._own()
                if log.assistant_msg_index >= 0:
                    out.turns.append(log)
            if finished is not None:
                out.exit_reason, out.needs_spec = "COMPLETED", finished == "NEEDS_SPEC"
                return
            turn += 1

    def _limit(
        self, turn: int, messages: list[dict], last: TurnLog | None, deadline: float
    ) -> tuple[str, str] | None:
        if turn >= self.limits.max_steps:
            return "TURN_LIMIT", "max_steps"
        if self.clock() >= deadline:
            return "TIMEOUT", "max_wall_seconds"
        if last is None:
            return None
        prompt, completion, _ = last.tokens
        if prompt is not None and completion is not None:
            if prompt + completion >= self.limits.context_cap:
                return "TURN_LIMIT", "context_cap"
        elif len(json.dumps(messages)) // 2 >= self.limits.context_cap:
            return "TURN_LIMIT", "context_cap_bytes_estimate"  # no usage: 2 bytes/token, early
        return None

    def _payload(self, messages: list[dict]) -> dict:
        s = self.settings
        payload = {
            "model": s.model,
            "messages": messages,
            "tools": TOOL_SCHEMAS,
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "temperature": s.temperature,
            "max_tokens": self.limits.max_completion_tokens,
        }
        if s.seed is not None:
            payload["seed"] = s.seed
        return payload

    def _step(self, log: TurnLog, messages: list[dict], deadline: float) -> str | None:
        """One turn; returns the finish status when the model finished, else None."""
        resp = self.endpoint.chat(self._payload(messages), deadline)
        *tokens, log.usage_status = parse_usage(resp)
        log.tokens = (tokens[0], tokens[1], tokens[2])
        log.usage_raw, log.server_model = resp.get("usage"), _str_or_none(resp.get("model"))
        msg = _message(resp)
        calls = msg.get("tool_calls") or []
        if not isinstance(calls, list) or not all(isinstance(c, dict) for c in calls):
            raise EndpointError("tool_calls is not a list of objects")
        assistant: dict[str, Any] = {"role": "assistant", "content": msg.get("content")}
        messages.append(assistant)
        log.assistant_msg_index = len(messages) - 1
        if not calls:
            messages.append({"role": "user", "content": NUDGE})
            return None
        call = dict(calls[0], id=calls[0].get("id") or f"call_{log.index}")
        assistant["tool_calls"] = [call]  # extra calls in one reply are dropped, never run
        log.extra_tool_calls_ignored = len(calls) - 1
        try:
            name, args = decode_call(call, len(self.task.public_tests))
        except BadCall as exc:
            log.action_type, log.error_category = "invalid_tool_call", "INVALID_CALL"
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": str(exc)})
            return None
        log.action_type, log.tool_args = name, args
        if name == "finish":
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": "finished"})
            return args["status"]
        text = self._execute(log, name, args, deadline)
        messages.append({"role": "tool", "tool_call_id": call["id"], "content": text})
        return None

    def _dispatch(self, name: str, args: dict) -> ToolCall:
        t = self.tools
        if name == "list_dir":
            return t.list_dir(args["path"])
        if name == "read_file":
            return t.read_file(args["path"])
        if name == "write_file":
            return t.write_file(args["path"], args["content"])
        if name == "edit_file":
            return t.edit_file(args["path"], args["target"], args["replacement"])
        index = args.get("target_index")
        targets = self.task.public_tests if index is None else (self.task.public_tests[index],)
        return t.run_tests([*RUN_TESTS_ARGS, *targets])

    def _execute(self, log: TurnLog, name: str, args: dict, deadline: float) -> str:
        left = deadline - self.clock() - 2 * KILL_GRACE_SECONDS
        if left <= 0:
            log.error_category = "TIMEOUT"
            raise TimeoutError("deadline passed before the tool call")
        self.tools.timeout_seconds = min(self.limits.tool_cap_seconds, left)
        self.tools.turn_index = log.index
        call = self._dispatch(name, args)
        log.exit_code, log.error_category = call.exit_code, call.error_category
        log.stdout_bytes, log.stdout_truncated = call.stdout_bytes, call.stdout_truncated
        log.duration_ms, log.exec_identity, log.isolation = (
            call.duration_ms,
            call.exec_identity,
            call.isolation,
        )
        log.action_type = call.action_type  # run_tests is recorded as the spec's "run_test"
        # The status query is one of the reserve's three steps (sweep and numstat the others),
        # so a turn ending at the deadline still observes its tree within max_wall_seconds.
        log.tree = self.observe(self.limits.step_seconds)
        if not log.tree.observed:
            raise _StatusUnavailable(f"git status failed after turn {log.index}")
        return render(call)

    def _cleanup(self, out: LoopOutcome) -> None:
        self.tools.timeout_seconds = self.limits.step_seconds
        try:
            sweep = self.tools.sweep()
            stat = self.tools.numstat()
        except WorkerIsolationError as exc:
            out.cleanup = "failed"
            self.abort_detail = self.abort_detail or f"cleanup: {exc}"
            return
        out.patch = parse_numstat(stat.raw) if stat.exit_code == 0 else None
        out.cleanup = "ok" if sweep.exit_code == 0 and out.patch is not None else "failed"


def _message(resp: dict) -> dict:
    choices = resp.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else None
    msg = first.get("message") if isinstance(first, dict) else None
    if not isinstance(msg, dict):
        raise EndpointError("response has no choices[0].message object")
    return msg


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None
