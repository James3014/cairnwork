"""Minimal local agent loop for the local worker arm (#88, Plan v2 on the Issue).

The loop runs as the Owner and talks to one loopback chat endpoint (endpoint.py). It never opens a
file: every byte it hands the model is ``ToolCall.output`` from the worker tools (tools.py, each
call ``sudo -n -u _cairnwork_worker`` + Seatbelt), and the only file I/O of a run is corpus_io.py
(task.json, the canary report, the output directory). After every tool call the worker runs the
fixed ``status`` query; the observed tree decides the cut (episode.first_edit_cut).

Limits (frozen in the manifest): ``max_steps`` turns (one model reply plus at most one tool call;
extra tool calls in a reply are dropped and counted), one deadline for the whole episode
(``max_wall_seconds`` minus a cleanup reserve), and a context cap on the last reply's prompt +
completion tokens. Every endpoint request gets the deadline itself (endpoint.py: a watchdog, single
receives and a check after the read). Every wrapper call gets an absolute deadline too
(tools.WorkerTools.deadline): its timeout leaves room for the kill grace a timed-out call may still
use (tools.KILL_GRACE_SECONDS, SIGTERM then SIGKILL) and, for a tool action, for the sweep after it
with its own grace; both wrapper calls of edit_file share one timeout. A tool call ends by the loop
deadline; the status query after it, the sweep and numstat each get a third of the cleanup reserve,
and nothing runs past start + max_wall_seconds.

``run_tests`` takes no free-form arguments: it runs ``pytest -q --tb=short -- <targets>`` over the
task's declared public test targets, or the one the model picks by ``target_index``.

Once the loop starts, ``run`` never raises: a reply that breaks the tool-call structure
(agent_protocol.ProtocolError) and any unexpected error end the episode as a recorded ERROR_ABORT,
and the cleanup (sweep, numstat) always runs, in a ``finally``. The first SIGTERM/SIGINT (cli.py
installs ``interrupt``) stops the loop as "interrupted"; later ones are only counted
(``late_signals``, in the private transcript); a SystemExit is handed to the caller to re-raise
after the line is written (review of #239, NB8). A call the model can repair
(agent_protocol.BadCall: an unknown tool, a missing or extra argument) is an invalid_tool_call.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from experimental.local_worker.agent_protocol import (
    NUDGE,
    RUN_TESTS_ARGS,
    SYSTEM_PROMPT,
    TOOL_SCHEMAS,
    BadCall,
    ProtocolError,
    decode_call,
    render,
)
from experimental.local_worker.endpoint import (
    Endpoint,
    EndpointError,
    EndpointTimeout,
    first_message,
    parse_usage,
)
from experimental.local_worker.episode import LoopOutcome, TurnLog
from experimental.local_worker.gitstate import TreeState, parse_numstat, parse_tracked
from experimental.local_worker.gitstate import parse_status as _parse_status
from experimental.local_worker.loop_config import Limits as Limits  # re-exported
from experimental.local_worker.loop_config import Settings, Task
from experimental.local_worker.tools import (
    ToolCall,
    ToolDeadline,
    WorkerIsolationError,
    WorkerTools,
)

UTC8 = timezone(timedelta(hours=8))  # spec §2: timestamps in UTC+8


class RefusedStart(RuntimeError):
    """The checkout is not its clean base (see the package docstring): no episode is run."""


class _StatusUnavailable(RuntimeError):
    pass


_ABORT_CAUSES = (  # checked in order; any other Exception is an internal_error
    (EndpointError, "endpoint_error"),
    (WorkerIsolationError, "worker_isolation"),
    (_StatusUnavailable, "status_unavailable"),
    (ProtocolError, "protocol_error"),
)


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
        tools.clock = clock  # every wrapper call is held to deadlines on this clock
        self.abort_detail: str | None = None
        self.interruptible = True  # NB8 (review of #239): see interrupt and _stopped
        self.late_signals = 0
        self.deferred: BaseException | None = None
        self._open: TurnLog | None = None  # the turn in progress (in out.turns, no t_end yet)
        self._start = 0.0
        self._offset = 0.0
        self._final = float("inf")  # start + max_wall_seconds: nothing runs past it

    def _elapsed(self) -> float:
        """Seconds on the logical episode clock (a resume starts at the parent's pre-cut time):
        what the limits use."""
        return self.clock() - self._start

    def _own(self) -> float:
        """Seconds this run itself has spent: what every recorded time uses (TurnLog t_*)."""
        return self._elapsed() - self._offset

    def _step_deadline(self) -> None:
        """One bookkeeping step (status, sweep or numstat): a third of the cleanup reserve, its
        kill grace included, and never past start + max_wall_seconds."""
        self.tools.timeout_seconds = self.limits.step_seconds
        step = self.limits.cleanup_reserve_seconds / 3
        self.tools.deadline = min(self._final, self.clock() + step)

    def observe(self) -> TreeState:
        """The worker's git status now; not observed unless it exited 0 with its whole output
        captured (a capped capture is never parsed as complete: Claude review of #239, NB5)."""
        self._step_deadline()
        call = self.tools.status()
        if call.exit_code != 0 or not call.capture_complete:
            return TreeState(observed=False)
        return _parse_status(call.raw)

    def _base_paths(self) -> frozenset[str] | None:
        """The base commit's paths (which path names the line may publish: redact.py); None
        when the query failed or its output was capped, and then every path is a digest."""
        self._step_deadline()
        call = self.tools.tracked()
        complete = call.exit_code == 0 and call.capture_complete
        return parse_tracked(call.raw) if complete else None

    def run(
        self,
        messages: list[dict],
        *,
        first_turn: int = 0,
        elapsed_offset: float = 0.0,
        expected_base: str | None = None,
    ) -> LoopOutcome:
        """Run from ``messages`` (a fresh start or a resume prefix) until a stop, recorded in the
        outcome. A SIGTERM/SIGINT before the loop starts (base observe, tracked query) escapes as
        KeyboardInterrupt and no line is written: no turn has run. Once a stop is recorded (or
        _stopped is recording one), a signal is only counted and that stop stands."""
        lim = self.limits
        self._start = self.clock() - elapsed_offset
        self._offset = elapsed_offset
        self._final = self._start + lim.max_wall_seconds
        deadline = self._final - lim.cleanup_reserve_seconds
        base = self.observe()
        if not base.observed or base.entries or base.head is None:
            raise RefusedStart("checkout is not a clean git tree at its base")
        if expected_base is not None and base.head != expected_base:
            raise RefusedStart(f"checkout HEAD {base.head} is not the pinned base {expected_base}")
        started = self.now().astimezone(UTC8).isoformat()
        out = LoopOutcome(base=base, base_paths=self._base_paths(), started_at=started)
        pending: BaseException | None = None  # what stopped the loop, until _stopped records it
        try:
            try:
                self._loop(messages, out, first_turn, deadline)  # clears interruptible on a stop
            except BaseException as exc:  # noqa: BLE001 - every stop is recorded (_stopped)
                pending = exc
                self._stopped(out, exc)
        except KeyboardInterrupt as exc:  # the one interrupt, landing as _stopped was entered
            self.late_signals += pending is not None  # then it only interrupted the recording
            self._stopped(out, pending or exc)
        finally:  # interruptible is False here: nothing below can be interrupted
            if self._open is not None:  # the turn an interrupt or error stopped ends now
                self._open.t_end = self._own()
            out.turns = [t for t in out.turns if t.assistant_msg_index >= 0]  # the model replied
            self._cleanup(out)
            out.ended_at = self.now().astimezone(UTC8).isoformat()
            out.wall_seconds = self._own()
        return out

    def interrupt(self, signum: int, frame: Any) -> None:
        """SIGTERM/SIGINT handler (cli.py installs it): KeyboardInterrupt the first time while
        the loop runs (recorded as interrupted), else only counted (late_signals)."""
        if not self.interruptible:
            self.late_signals += 1
            return
        self.interruptible = False
        raise KeyboardInterrupt

    def _stopped(self, out: LoopOutcome, exc: BaseException) -> None:
        """Record why the loop stopped. A SystemExit (also one an interrupt replaced) is deferred to
        the caller; any other non-Exception BaseException is recorded "interrupted", not raised."""
        self.interruptible = False  # the loop has stopped: from here on a signal is counted
        if isinstance(exc, (EndpointTimeout, TimeoutError)):
            out.exit_reason, out.timed_out, out.limit_hit = "TIMEOUT", True, "max_wall_seconds"
            return
        if isinstance(exc, Exception):
            cause = next((c for kind, c in _ABORT_CAUSES if isinstance(exc, kind)), None)
            self._abort(out, cause or "internal_error", exc)
            return
        out.exit_reason, out.interrupted, out.abort_cause = "ERROR_ABORT", True, "interrupted"
        # W2 (review of PR #248): an interrupt raised while a SystemExit unwound replaced it;
        # only that SystemExit is deferred, never an Exception or a GeneratorExit context (W4)
        held = exc.__context__ if isinstance(exc, KeyboardInterrupt) else exc
        if isinstance(held, SystemExit):
            self.deferred = self.deferred or held

    def _abort(self, out: LoopOutcome, cause: str, exc: Exception) -> None:
        out.exit_reason, out.abort_cause = "ERROR_ABORT", cause
        self.abort_detail = f"{type(exc).__name__}: {exc}"

    def _loop(self, messages: list[dict], out: LoopOutcome, turn: int, deadline: float) -> None:
        while True:
            stop = self._limit(turn, messages, out.turns[-1] if out.turns else None, deadline)
            if stop:
                self.interruptible = False  # a stop is recorded: a signal is only counted now
                out.exit_reason, out.limit_hit = stop
                out.timed_out = stop[0] == "TIMEOUT"
                return
            log = TurnLog(index=turn, t_start=self._own())
            self._open = log  # recorded before it runs: an interrupt anywhere (also after its
            out.turns.append(log)  # tool ran) leaves it in out.turns (Codex review of PR #248)
            finished = self._step(log, messages, deadline)
            log.t_end, self._open = self._own(), None
            if finished is not None:  # _step already cleared interruptible
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
        log.usage_raw, model = resp.get("usage"), resp.get("model")
        log.server_model = model if isinstance(model, str) else None
        msg = first_message(resp)
        calls = msg.get("tool_calls") or []
        assistant: dict[str, Any] = {"role": "assistant", "content": msg.get("content")}
        messages.append(assistant)
        log.assistant_msg_index = len(messages) - 1
        if not calls:
            messages.append({"role": "user", "content": NUDGE})
            return None
        if not isinstance(calls, list) or not all(isinstance(c, dict) for c in calls):
            log.action_type, log.error_category = "invalid_tool_call", "PROTOCOL_ERROR"
            raise ProtocolError("tool_calls is not a list of objects")
        assistant["tool_calls"] = calls[:1]  # extra calls in one reply are dropped, never run
        log.extra_tool_calls_ignored = len(calls) - 1
        try:
            name, args = decode_call(calls[0], len(self.task.public_tests))
        except ProtocolError:
            log.action_type, log.error_category = "invalid_tool_call", "PROTOCOL_ERROR"
            raise
        except BadCall as exc:
            name, args, bad = "", {}, exc
        else:
            bad = None
        # One id for the assistant's call and the tool reply, also for a BadCall (review of
        # #239, NB7); decode_call already refused an id that is not a string.
        call = dict(calls[0], id=calls[0].get("id") or f"call_{log.index}")
        assistant["tool_calls"] = [call]
        if bad is not None:
            log.action_type, log.error_category = "invalid_tool_call", "INVALID_CALL"
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": str(bad)})
            return None
        if name == "finish":  # no interrupt once a turn is a finish: it can only be COMPLETED
            self.interruptible = False  # (review of PR #248, W1); a signal is only counted now
            log.action_type, log.tool_args = name, args
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": "finished"})
            return args["status"]
        log.action_type = "run_test" if name == "run_tests" else name  # the spec's action name
        log.tool_args = args
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
        # Every wrapper call of the tool (both of edit_file's, the sweep after a pytest run or a
        # timeout, and their kill graces) ends by the loop deadline: tools.WorkerTools._timeout.
        self.tools.timeout_seconds = self.limits.tool_cap_seconds
        self.tools.deadline = deadline
        self.tools.turn_index = log.index
        started = self.tools.calls_started
        try:
            call = self._dispatch(name, args)
        except ToolDeadline:
            log.error_category = "TIMEOUT"
            raise
        finally:  # a started wrapper process may have changed the tree, also when it then failed
            log.tool_ran = self.tools.calls_started > started
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
        log.tree = self.observe()
        if not log.tree.observed:
            raise _StatusUnavailable(f"git status failed after turn {log.index}")
        return render(call)

    def _cleanup(self, out: LoopOutcome) -> None:
        """Sweep, numstat and the last tree, as far as they get: whatever stops them (even
        SystemExit) leaves cleanup "failed" and the line still gets written."""
        try:
            self._cleanup_steps(out)
        except BaseException as exc:  # noqa: BLE001 - the caller re-raises it after the line
            out.cleanup = "failed"
            self.abort_detail = self.abort_detail or f"cleanup: {type(exc).__name__}: {exc}"
            if not isinstance(exc, Exception):
                self.deferred = self.deferred or exc

    def _cleanup_steps(self, out: LoopOutcome) -> None:
        try:
            self._step_deadline()
            sweep = self.tools.sweep()
            self._step_deadline()
            stat = self.tools.numstat()
        except (WorkerIsolationError, ToolDeadline) as exc:
            out.cleanup = "failed"
            self.abort_detail = self.abort_detail or f"cleanup: {exc}"
            return
        whole = stat.exit_code == 0 and stat.capture_complete  # a capped count is no count
        out.patch = parse_numstat(stat.raw) if whole else None
        out.cleanup = "ok" if sweep.exit_code == 0 and out.patch is not None else "failed"
        self._observe_last(out)

    def _observe_last(self, out: LoopOutcome) -> None:
        """The run stopped after a tool started but before its tree was observed: observe it now,
        after the sweep (nothing else ran since), so its cut is known; else it stays unobserved."""
        last = out.turns[-1] if out.turns else None
        if last is None or not last.tool_ran or (last.tree is not None and last.tree.observed):
            return
        try:
            last.tree = self.observe()
        except (WorkerIsolationError, ToolDeadline):
            pass
