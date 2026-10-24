"""Run or resume one local worker episode (#88) and append its corpus line.

    python -m experimental.local_worker.cli run --task task.json \\
        --checkout /Users/Shared/cairnwork-worker/local-runs/<32 hex>/checkout \\
        --out ~/.local/state/cairnwork-88/episodes --model <served model id> --context-cap 65536 \\
        --canary-report <out>/canary/local-worker-canary-worker-<stamp>.json [--arm L0 --seed 0]
    python -m experimental.local_worker.cli resume --parent-transcript \\
        <out>/private/<episode_id>/transcript.json [--cut-turn N] [--plan <out>/plans/x.txt] \\
        --task task.json --checkout <a FRESH run root>/checkout ... --arm A --seed 1000

Before anything runs: the canary report is bound (corpus_io.verify_canary), the server must list
the requested model, and the manifest is created or must match. A resume starts from a fresh
checkout that must be clean at the parent's base sha; the prefix stops just before the parent's
cut turn, so the tree there is base by construction (Plan v2 point 5). Ignored files: the status
query lists them (--ignored=matching), so an ignored artifact that run_tests writes into the
checkout is itself a tree change and sets the cut; none lies before it. What run_tests left before
the cut outside the checkout (<run_root>/tmp, its HOME and TMPDIR) is not recreated in the fresh
run root. Exit 0 after the line is written (whatever the episode's exit reason), 2 when a check
refused the start.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import signal
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experimental.local_worker import corpus_io
from experimental.local_worker.agent import (
    AgentLoop,
    Limits,
    RefusedStart,
    Settings,
    Task,
    initial_messages,
)
from experimental.local_worker.agent_protocol import (
    RUN_TESTS_ARGS,
    SYSTEM_PROMPT,
    TOOL_SCHEMAS,
    TOOLS_ABSENT,
)
from experimental.local_worker.endpoint import DEFAULT_URL, EndpointError, LoopbackEndpoint
from experimental.local_worker.episode import (
    NULL_RULE,
    PARENT_FIELDS,
    SCHEMA_VERSION,
    TOKENS_ACCUM_FORMULA,
    LoopOutcome,
    build_record,
    parent_prefix,
)
from experimental.local_worker.tools import WorkerTools

DEFAULT_OUT = Path.home() / ".local/state/cairnwork-88/episodes"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def build_manifest(a: argparse.Namespace, limits: Limits, endpoint: str) -> dict:
    return {
        "schema": MANIFEST_SCHEMA,
        "episode_schema_version": SCHEMA_VERSION,
        "harness_version": corpus_io.harness_version(),
        "model_name": a.model,
        "endpoint": endpoint,
        "server_model_id": a.model,  # confirmed listed by the server's /models
        "limits": asdict(limits),
        "temperature": a.temperature,
        "tools": [t["function"]["name"] for t in TOOL_SCHEMAS],
        "tools_absent": list(TOOLS_ABSENT),
        "run_tests_command": ["pytest", "-p", "no:cacheprovider", *RUN_TESTS_ARGS, "<targets>"],
        "worker_prompt_sha256": _sha(SYSTEM_PROMPT),
        "tool_schemas_sha256": _sha(json.dumps(TOOL_SCHEMAS, sort_keys=True)),
        "null_rule": NULL_RULE,
        "tokens_accum_formula": TOKENS_ACCUM_FORMULA,
        "canary": a.canary_binding,
        "created_at": datetime.now(UTC).isoformat(),
    }


MANIFEST_SCHEMA = corpus_io.MANIFEST_SCHEMA


def resume_prefix(parent: dict, cut_turn: int | None) -> tuple[list[dict], int, float]:
    """(messages before the cut turn's model reply, cut turn index, its start in seconds).

    The resumed run's clock starts at that start (wall_clock_seconds includes it; its
    own_wall_seconds does not). Ignored artifacts in the checkout cannot precede the cut (status
    lists ignored files, so creating one is the cut); what run_tests left before the cut outside
    the checkout (<run_root>/tmp) is not recreated in the fresh run root."""
    cut = parent["record"]["first_edit_cut"]
    limit: int | None = cut["cut_turn_index"] if cut["cut_occurred"] else None
    chosen = cut_turn if cut_turn is not None else limit
    if chosen is None:
        raise corpus_io.InputRefused("the parent has no cut: pass --cut-turn")
    if limit is not None and chosen > limit:
        raise corpus_io.InputRefused(f"turn {chosen} is after the cut ({limit}): tree not base")
    turn = next((t for t in parent["turns"] if t["index"] == chosen), None)
    if turn is None or turn["assistant_msg_index"] < 0:
        raise corpus_io.InputRefused(f"the parent has no turn {chosen}")
    return parent["messages"][: turn["assistant_msg_index"]], chosen, float(turn["t_start"])


def transcript(eid: str, task: Task, messages: list, out: LoopOutcome, loop: AgentLoop) -> dict:
    turns = []
    for t in out.turns:
        row = {k: v for k, v in asdict(t).items() if k != "tree"}
        row["tree"] = None if t.tree is None else {"head": t.tree.head, "entries": t.tree.entries}
        turns.append(row)
    return {
        "episode_id": eid,
        "task": asdict(task),
        "messages": messages,
        "turns": turns,
        "abort_detail": loop.abort_detail,
    }


def _prepare(a: argparse.Namespace) -> dict[str, Any]:
    out_dir = a.out.expanduser().resolve()
    checkout = a.checkout.resolve()
    if checkout.name != "checkout":
        raise corpus_io.InputRefused("--checkout must be <run_root>/checkout")
    task = corpus_io.load_task(a.task)
    if not corpus_io.ID_RE.fullmatch(a.arm):
        raise corpus_io.InputRefused(f"--arm must match {corpus_io.ID_RE.pattern}")
    a.canary_binding = corpus_io.verify_canary(a.canary_report, out_dir)
    endpoint = LoopbackEndpoint(a.endpoint)
    ids = endpoint.models(time.monotonic() + 30)
    if a.model not in ids:
        raise corpus_io.InputRefused(f"the server does not list model {a.model!r}: {ids}")
    limits = Limits(
        context_cap=a.context_cap,
        max_steps=a.max_steps,
        max_wall_seconds=a.max_wall_seconds,
        cut_seconds=a.cut_seconds,
        tool_cap_seconds=a.tool_cap_seconds,
        max_completion_tokens=a.max_completion_tokens,
    )
    manifest_sha = corpus_io.prepare_manifest(out_dir, build_manifest(a, limits, endpoint.url))
    return dict(
        out_dir=out_dir,
        run_root=checkout.parent,
        task=task,
        endpoint=endpoint,
        limits=limits,
        manifest_sha=manifest_sha,
    )


def _episode_id(a: argparse.Namespace, task: Task) -> str:
    model = "".join(c if c.isalnum() else "_" for c in a.model)[:40]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"ep_{stamp}_{model}_{task.task_id}_{a.arm}_s{a.seed}_{secrets.token_hex(3)}"


def _on_sigterm(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt  # recorded as interrupted; cleanup and the line still happen


def run_episode(a: argparse.Namespace, resume: bool) -> int:
    p = _prepare(a)
    task: Task = p["task"]
    messages = initial_messages(task)
    first_turn, offset, expected_base, parent_id = 0, 0.0, None, None
    plan_sha, prefix = None, dict.fromkeys(PARENT_FIELDS)
    if resume:
        parent = corpus_io.load_transcript(a.parent_transcript, p["out_dir"])
        if parent["task"]["task_id"] != task.task_id:
            raise corpus_io.InputRefused("the parent episode is for another task")
        if parent["record"]["resume_parent_episode_id"] is not None:
            raise corpus_io.InputRefused("resume from a fresh (L0) episode, not from a resume run")
        messages, first_turn, offset = resume_prefix(parent, a.cut_turn)
        prefix = parent_prefix(parent["turns"], first_turn)
        expected_base, parent_id = parent["record"]["base_sha"], parent["episode_id"]
        if a.plan:
            plan = corpus_io.load_plan(a.plan, p["out_dir"])
            plan_sha = _sha(plan)
            messages.append({"role": "user", "content": plan})
    eid = _episode_id(a, task)
    priv = corpus_io.private_dir(p["out_dir"], eid)
    tools = WorkerTools(p["run_root"], priv / "calls.jsonl", mode="worker")
    settings = Settings(a.model, p["limits"], a.seed, a.temperature)
    loop = AgentLoop(tools, p["endpoint"], task, settings)
    signal.signal(signal.SIGTERM, _on_sigterm)
    out = loop.run(
        messages, first_turn=first_turn, elapsed_offset=offset, expected_base=expected_base
    )
    meta = {
        "episode_id": eid,
        "task_id": task.task_id,
        "repo": task.repo,
        "model_name": a.model,
        "harness_version": corpus_io.harness_version(),
        "checkout_id": p["run_root"].name,
        "arm": a.arm,
        "seed": a.seed,
        "resume_parent_episode_id": parent_id,
        "resume_cut_turn_index": first_turn if resume else None,
        "plan_sha256": plan_sha,
        **prefix,
        "endpoint": p["endpoint"].url,
        "server_model_id": next((t.server_model for t in out.turns if t.server_model), None),
        "manifest_sha256": p["manifest_sha"],
        "canary_report_sha256": a.canary_binding["report_sha256"],
    }
    record = build_record(meta, out, p["limits"].cut_seconds)
    private = transcript(eid, task, messages, out, loop)
    private["record"] = record
    corpus_io.write_private_json(priv, "transcript.json", private)
    path = corpus_io.episodes_path(p["out_dir"], a.dataset, a.model, a.split)
    line_sha = corpus_io.append_episode(path, record, p["out_dir"])
    print(f"LOCAL_WORKER_EPISODE {out.exit_reason} {eid} {path} line_sha256={line_sha}")
    return 0


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Local worker agent loop and episode recorder (#88).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "resume"):
        sp = sub.add_parser(name)
        sp.add_argument("--task", type=Path, required=True)
        sp.add_argument("--checkout", type=Path, required=True)
        sp.add_argument("--out", type=Path, default=DEFAULT_OUT)
        sp.add_argument("--model", required=True)
        sp.add_argument("--context-cap", type=int, required=True)
        sp.add_argument("--canary-report", type=Path, required=True)
        sp.add_argument("--endpoint", default=DEFAULT_URL)
        sp.add_argument("--dataset", default="cairnwork")
        sp.add_argument("--split", default="pilot")
        sp.add_argument("--arm", default="L0" if name == "run" else "A")
        sp.add_argument("--seed", type=int, default=None)
        sp.add_argument("--temperature", type=float, default=0.0)
        sp.add_argument("--max-steps", type=int, default=40)
        sp.add_argument("--max-wall-seconds", type=float, default=1800.0)
        sp.add_argument("--cut-seconds", type=float, default=900.0)
        sp.add_argument("--tool-cap-seconds", type=float, default=300.0)
        sp.add_argument("--max-completion-tokens", type=int, default=4096)
        if name == "resume":
            sp.add_argument("--parent-transcript", type=Path, required=True)
            sp.add_argument("--cut-turn", type=int, default=None)
            sp.add_argument("--plan", type=Path, default=None)
    return ap


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    try:
        return run_episode(a, resume=a.cmd == "resume")
    except (corpus_io.InputRefused, RefusedStart, EndpointError, ValueError) as exc:
        print(f"LOCAL_WORKER_EPISODE_REFUSED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
