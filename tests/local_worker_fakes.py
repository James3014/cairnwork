"""Fakes for the local worker loop tests (#88): an in-memory checkout behind a wrapper-like invoker,
a scripted chat endpoint and a settable clock. No model, no sudo, no network."""

from __future__ import annotations

import copy
import difflib
import itertools
import json
from pathlib import Path
from typing import Any

from experimental.local_worker import cli, corpus_io, tools
from experimental.local_worker.agent import AgentLoop, Limits, Settings, Task
from experimental.local_worker.endpoint import EndpointTimeout
from experimental.local_worker.episode import PARENT_FIELDS
from experimental.local_worker.tools import WorkerTools

BASE_SHA = "a" * 40
TASK = Task("TASK_01", "repo_x", "SECRET-PROMPT-7f3 fix the bug", ("tests/test_app.py",))
META: dict[str, Any] = {
    "episode_id": "ep_test",
    "task_id": "TASK_01",
    "repo": "repo_x",
    "model_name": "served-model",
    "harness_version": "0" * 40,
    "checkout_id": "0" * 32,
    "arm": "L0",
    "seed": 7,
    "resume_parent_episode_id": None,
    "resume_cut_turn_index": None,
    "plan_sha256": None,
    **dict.fromkeys(PARENT_FIELDS),
    "endpoint": "http://127.0.0.1:8000/v1",
    "server_model_id": "served-model",
    "manifest_sha256": "1" * 64,
    "canary_report_sha256": "2" * 64,
}


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class World:
    """Replies like local-worker-tool over a dict checkout; status/numstat derive from the dict."""

    def __init__(self, files: dict[str, str] | None = None, clock: Clock | None = None) -> None:
        self.base = dict(files or {"src/app.py": "x = 1\n", "tests/test_app.py": "def test(): 0\n"})
        self.files = dict(self.base)
        self.pytest_writes: dict[str, str] = {}
        self.pytest_output = b"SECRET-OUTPUT-91 1 failed\n"
        self.status_rc = 0
        self.head = BASE_SHA
        self.calls: list[tuple[str, list[str], float]] = []
        self.clock, self.tool_seconds = clock, 0.0

    def __call__(self, argv: list[str], stdin: bytes | None, timeout: float) -> Any:
        i = argv.index("--start-nonce")
        nonce, action, args = argv[i + 1], argv[i + 2], argv[i + 4 :]
        self.calls.append((action, args, timeout))
        if self.clock is not None:
            self.clock.t += self.tool_seconds
        rc, out = self.handle(action, args, stdin or b"")
        return rc, out, f"{tools.START_MARKER} {nonce}\n".encode()

    def handle(self, action: str, args: list[str], stdin: bytes) -> tuple[int, bytes]:
        if action == "list":
            return 0, "\n".join(sorted(self.files)).encode()
        if action == "read":
            if args[0] not in self.files:
                return 64, b"local-worker-tool: refusing: not a regular file\n"
            return 0, self.files[args[0]].encode()
        if action == "write":
            if args[0].startswith("../"):
                return 64, b"local-worker-tool: refusing: path\n"
            self.files[args[0]] = stdin.decode()
            return 0, b""
        if action == "pytest":
            self.files.update(self.pytest_writes)
            return 1, self.pytest_output
        if action == "sweep":
            return 0, b"LOCAL_WORKER_SWEEP clean\n"
        if action == "status":
            return self.status_rc, self.status()
        if action == "numstat":
            return 0, self.numstat()
        return 64, b"refusing\n"

    def changed(self) -> list[str]:
        keys = set(self.files) | set(self.base)
        return sorted(k for k in keys if self.files.get(k) != self.base.get(k))

    def status(self) -> bytes:
        recs = [f"# branch.oid {self.head}", "# branch.head main"]
        for p in self.changed():
            if p in self.base:
                recs.append(f"1 .M N... 100644 100644 100644 {'b' * 40} {'b' * 40} {p}")
            elif p.endswith(".log"):  # .gitignore'd: listed by --ignored=matching
                recs.append(f"! {p}")
            else:
                recs.append(f"? {p}")
        return ("\0".join(recs) + "\0").encode()

    def numstat(self) -> bytes:
        out = []
        for p in self.changed():  # like the wrapper: staged with --force, ignored files count
            old = self.base.get(p, "").splitlines()
            new = self.files.get(p, "").splitlines()
            diff = list(difflib.ndiff(old, new))
            add = sum(d.startswith("+ ") for d in diff)
            rem = sum(d.startswith("- ") for d in diff)
            out.append(f"{add}\t{rem}\t{p}")
        return ("\0".join(out) + ("\0" if out else "")).encode()


def reply(
    name: str | None = None,
    usage: Any = (100, 10, 50),
    n_calls: int = 1,
    say: str | None = "SECRET-THOUGHT-55",
    **args: Any,
) -> dict:
    msg: dict[str, Any] = {"role": "assistant", "content": say}
    if name:
        call = {"type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        msg["tool_calls"] = [dict(call, id=f"c{k}") for k in range(n_calls)]
    resp: dict[str, Any] = {"model": "served-model", "choices": [{"message": msg}]}
    if usage == "missing":
        return resp
    p, c, cached = usage
    resp["usage"] = {"prompt_tokens": p, "completion_tokens": c}
    if cached is not None:
        resp["usage"]["prompt_tokens_details"] = {"cached_tokens": cached}
    return resp


class FakeEndpoint:
    url = "http://127.0.0.1:8000/v1"

    def __init__(self, replies: list, clock: Clock | None = None, seconds: float = 0.0) -> None:
        self.replies, self.clock, self.seconds = list(replies), clock, seconds
        self.payloads: list[dict] = []
        self.deadlines: list[float] = []

    def chat(self, payload: dict, deadline: float) -> dict:
        self.payloads.append(copy.deepcopy(payload))
        self.deadlines.append(deadline)
        if self.clock is not None:
            if self.clock() >= deadline:  # like LoopbackEndpoint._remaining
                raise EndpointTimeout("deadline passed")
            if self.clock() + self.seconds > deadline:  # like its watchdog: no reply past it
                self.clock.t = deadline
                raise EndpointTimeout("endpoint passed the deadline")
            self.clock.t += self.seconds
        r = self.replies.pop(0) if self.replies else reply("finish", status="DONE")
        if isinstance(r, BaseException):
            raise r
        return r

    def models(self, deadline: float) -> list[str]:
        return ["served-model"]


def make_loop(tmp_path: Any, replies: list, world: World | None = None, **limits: Any):
    tmp_path.mkdir(parents=True, exist_ok=True)
    clock = Clock()
    world = world or World(clock=clock)
    world.clock = clock
    wt = WorkerTools(tmp_path / ("0" * 32), tmp_path / "calls.jsonl", invoker=world)
    endpoint = FakeEndpoint(replies, clock, limits.pop("model_seconds", 1.0))
    lim = Limits(context_cap=limits.pop("context_cap", 100_000), **limits)
    loop = AgentLoop(wt, endpoint, TASK, Settings("served-model", lim, seed=7), clock=clock)
    return loop, world, endpoint, clock


def cli_harness(tmp_path: Any, monkeypatch: Any):
    """The CLI with fake tools and endpoint injected: (run, out). Each run gets a fresh run root
    unless ``root`` names one (a 32-hex tag)."""
    out = tmp_path / "out"
    task = tmp_path / "task.json"
    task.write_text(
        json.dumps(
            {
                "task_id": "TASK_01",
                "repo": "repo_x",
                "prompt": "SECRET-PROMPT-7f3 fix it",
                "public_tests": ["tests/test_app.py"],
            }
        )
    )
    state: dict[str, Any] = {"replies": [], "world": None}
    binding = {"report_sha256": "c" * 64, "report": "r.json"}
    monkeypatch.setattr(corpus_io, "verify_canary", lambda path, out_dir: binding)
    monkeypatch.setattr(cli, "LoopbackEndpoint", lambda url: FakeEndpoint(state["replies"]))

    def tools_factory(run_root, log, mode):
        assert mode == "worker"
        state["world"] = World()
        return tools.WorkerTools(run_root, log, mode=mode, invoker=state["world"])

    monkeypatch.setattr(cli, "WorkerTools", tools_factory)
    tags = (f"{n:032x}" for n in itertools.count(1))

    def run(replies: list, *extra: str, root: str | None = None) -> int:
        state["replies"] = list(replies)
        checkout = tmp_path / "runs" / (root or next(tags)) / "checkout"
        checkout.mkdir(parents=True, exist_ok=True)
        args = ["--task", str(task), "--checkout", str(checkout), "--out", str(out)]
        args += ["--model", "served-model", "--context-cap", "65536", "--canary-report", "x"]
        return cli.main([*extra[:1], *args, *extra[1:]])

    return run, out


def lines(out: Path) -> list[dict]:
    files = sorted(out.glob("episodes-*.jsonl"))
    assert len(files) == 1
    return [json.loads(x) for x in files[0].read_text().splitlines()]
