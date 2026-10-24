"""#88 episode recorder, CLI, canary binding, loopback endpoint and the Owner-loop I/O boundary."""

from __future__ import annotations

import ast
import hashlib
import http.server
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from local_worker_fakes import BASE_SHA, FakeEndpoint, World, reply

from experimental.local_worker import cli, corpus_io, endpoint, episode, gitstate, tools
from experimental.local_worker.episode_schema import TOP_EXTRA, TOP_SPEC, SchemaError
from experimental.local_worker.episode_schema import validate_record as validate

SPEC_TOP = {  # EPISODE_CORPUS_SPEC_ZH §2, verbatim
    "episode_id",
    "task_id",
    "repo",
    "model_name",
    "harness_version",
    "wall_clock_start",
    "wall_clock_end",
    "wall_clock_seconds",
    "total_turns",
    "first_edit_cut",
    "turns",
    "verification",
    "spec_feedback",
}
SPEC_TURN = {
    "turn_index",
    "role",
    "action_type",
    "tool_args",
    "exit_code",
    "stdout_bytes",
    "stdout_truncated",
    "error_category",
    "tokens_prompt",
    "tokens_completion",
    "tokens_cached",
    "duration_ms",
}
SECRETS = ("SECRET-PROMPT-7f3", "SECRET-THOUGHT-55", "SECRET-OUTPUT-91", "SECRET-CONTENT-12")
L0_REPLIES = [
    reply("read_file", path="src/app.py"),
    reply("run_tests"),
    reply("write_file", path="src/app.py", content="x = 2  # SECRET-CONTENT-12\n"),
    reply("finish", status="DONE", summary="SECRET-THOUGHT-55"),
]


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """The CLI with fake tools and endpoint injected; returns a runner and the output dir."""
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

    def run(replies: list, *extra: str) -> int:
        state["replies"] = list(replies)
        checkout = tmp_path / ("0" * 32) / "checkout"
        checkout.mkdir(parents=True, exist_ok=True)
        args = ["--task", str(task), "--checkout", str(checkout), "--out", str(out)]
        args += ["--model", "served-model", "--context-cap", "65536", "--canary-report", "x"]
        return cli.main([*extra[:1], *args, *extra[1:]])

    return run, out


def lines(out: Path) -> list[dict]:
    files = sorted(out.glob("episodes-*.jsonl"))
    assert len(files) == 1
    return [json.loads(x) for x in files[0].read_text().splitlines()]


def test_run_writes_one_valid_line_with_the_spec_keys(harness):
    run, out = harness
    assert run(L0_REPLIES, "run", "--seed", "0") == 0
    [rec] = lines(out)
    validate(rec)
    assert SPEC_TOP <= set(rec) and set(rec) == set(TOP_SPEC) | set(TOP_EXTRA)
    assert all(SPEC_TURN <= set(t) for t in rec["turns"])
    assert rec["first_edit_cut"]["cut_turn_index"] == 2
    assert rec["base_sha"] == BASE_SHA and rec["arm"] == "L0" and rec["seed"] == 0
    assert rec["wall_clock_start"].endswith("+08:00")
    assert rec["turns"][2]["tool_args"]["content"]["bytes"] == len("x = 2  # SECRET-CONTENT-12\n")
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["limits"]["max_steps"] == 40 and manifest["tools_absent"] == ["search"]
    manifest_bytes = (out / "manifest.json").read_bytes()
    assert rec["manifest_sha256"] == hashlib.sha256(manifest_bytes).hexdigest()
    print("EXAMPLE_LINE " + json.dumps(rec, sort_keys=True))


def test_no_transcript_text_in_the_corpus_line(harness):
    run, out = harness
    run(L0_REPLIES, "run")
    text = next(out.glob("episodes-*.jsonl")).read_text()
    assert not [s for s in SECRETS if s in text]
    [priv] = (out / "private").iterdir()
    assert (out / "private").stat().st_mode & 0o777 == 0o700
    assert priv.stat().st_mode & 0o777 == 0o700
    private = (priv / "transcript.json").read_text()
    assert all(s in private for s in SECRETS)  # the raw details stay in the private run dir


def test_resume_from_the_cut_with_a_plan(harness):
    run, out = harness
    run(L0_REPLIES, "run")
    [parent] = lines(out)
    transcript = next((out / "private").iterdir()) / "transcript.json"
    (out / "plans").mkdir()
    (out / "plans" / "p.txt").write_text("PLAN: change x")
    extra = ["--parent-transcript", str(transcript), "--plan", str(out / "plans" / "p.txt")]
    assert run([reply("finish", status="DONE")], "resume", "--seed", "1000", *extra) == 0
    child = lines(out)[1]
    assert child["resume_parent_episode_id"] == parent["episode_id"]
    assert child["resume_cut_turn_index"] == 2 and child["turns"][0]["turn_index"] == 2
    assert child["plan_sha256"] == hashlib.sha256(b"PLAN: change x").hexdigest()
    sent = json.loads((out / "private" / child["episode_id"] / "transcript.json").read_text())
    msgs = sent["messages"]
    assert msgs[-3]["content"] == "PLAN: change x"  # then the reply and its tool message
    roles = [m["role"] for m in msgs[:6]]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "tool"]
    assert child["turns"][0]["t_start_seconds"] == parent["turns"][2]["t_start_seconds"]
    offset = child["wall_clock_seconds"] - child["own_wall_seconds"]  # the parent's pre-cut time
    assert offset == pytest.approx(parent["first_edit_cut"]["pre_cut_wall_seconds"], abs=0.002)


def test_resume_refuses_a_turn_after_the_cut(harness):
    run, out = harness
    run(L0_REPLIES, "run")
    transcript = next((out / "private").iterdir()) / "transcript.json"
    extra = ["--parent-transcript", str(transcript), "--cut-turn", "3"]
    assert run([], "resume", *extra) == 2


def test_no_canary_no_start(harness, monkeypatch):
    run, out = harness

    def refuse(path, out_dir):
        raise corpus_io.InputRefused("no canary")

    monkeypatch.setattr(corpus_io, "verify_canary", refuse)
    assert run(L0_REPLIES, "run") == 2
    assert not list(out.glob("episodes-*.jsonl")) and not (out / "manifest.json").exists()


def test_manifest_is_frozen(harness):
    run, out = harness
    run(L0_REPLIES, "run")
    assert run(L0_REPLIES, "run", "--max-steps", "30") == 2
    assert len(lines(out)) == 1


@pytest.fixture
def canary(tmp_path):
    wrapper = tmp_path / "local-worker-tool"
    wrapper.write_bytes(corpus_io.REPO_WRAPPER.read_bytes())
    report = {
        "overall": "PASS",
        "mode": "worker",
        "wrapper": str(wrapper),
        "wrapper_sha256": hashlib.sha256(wrapper.read_bytes()).hexdigest(),
        "host": "mini",
        "finished_at": datetime.now(UTC).isoformat(),
    }

    def write(**changes: Any) -> Path:
        data = json.dumps({**report, **changes}).encode()
        path = tmp_path / "out" / "canary.json"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
        Path(str(path) + ".sha256").write_text(hashlib.sha256(data).hexdigest() + "  x\n")
        return path

    return write, wrapper, tmp_path / "out"


def test_canary_binding_accepts_a_fresh_worker_pass(canary):
    write, wrapper, out = canary
    binding = corpus_io.verify_canary(write(), out, wrapper=wrapper, host="mini")
    assert len(binding["report_sha256"]) == 64


@pytest.mark.parametrize(
    "changes",
    [
        {"overall": "FAIL"},
        {"mode": "direct"},
        {"wrapper_sha256": "0" * 64},
        {"host": "other"},
        {"finished_at": (datetime.now(UTC) - timedelta(days=8)).isoformat()},
    ],
)
def test_canary_binding_refuses(canary, changes):
    write, wrapper, out = canary
    with pytest.raises(corpus_io.InputRefused):
        corpus_io.verify_canary(write(**changes), out, wrapper=wrapper, host="mini")


def test_canary_binding_refuses_tampering_and_outside_paths(canary, tmp_path):
    write, wrapper, out = canary
    path = write()
    path.write_bytes(path.read_bytes() + b" ")  # same JSON, bytes differ from the sidecar
    with pytest.raises(corpus_io.InputRefused):
        corpus_io.verify_canary(path, out, wrapper=wrapper, host="mini")
    with pytest.raises(corpus_io.InputRefused):
        corpus_io.verify_canary(write(), tmp_path / "elsewhere", wrapper=wrapper, host="mini")
    wrapper.write_text("#!/bin/sh\n")  # installed copy differs from the repository's
    with pytest.raises(corpus_io.InputRefused):
        corpus_io.verify_canary(write(), out, wrapper=wrapper, host="mini")


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8000/v1",
        "http://localhost:8000/v1",
        "http://127.0.0.1/v1",
        "http://10.0.0.5:8000/v1",
        "http://127.0.0.1.nip.io:8000/v1",
        "http://user:pw@127.0.0.1:8000/v1",
        "http://127.0.0.1:8000@evil.example:80/v1",
        "http://127.1:8000/v1",
        "http://[::ffff:127.0.0.1]:8000/v1",
        "http://127.0.0.1:8000/v1?x=1",
    ],
)
def test_non_loopback_endpoints_are_refused(url):
    with pytest.raises(ValueError):
        endpoint.parse_loopback_url(url)


def test_loopback_literals_are_accepted():
    assert str(endpoint.parse_loopback_url("http://127.0.0.1:8000/v1/")) == endpoint.DEFAULT_URL
    assert endpoint.parse_loopback_url("http://[::1]:9/v1").host == "::1"
    assert not endpoint.peer_is_loopback(("192.168.1.2", 8000))


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/redirect/models":
            self.send_response(302)
            self.send_header("Location", "http://10.0.0.1:1/v1/models")
            self.end_headers()
            return
        body = json.dumps({"data": [{"id": "served-model"}]}).encode()
        self.send_response(200 if self.path == "/v1/models" else 500)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv.server_address[1]
    srv.shutdown()


def test_endpoint_talks_http_without_proxy_or_redirects(server, monkeypatch):
    monkeypatch.setenv("http_proxy", "http://10.0.0.1:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://10.0.0.1:3128")
    base = f"http://127.0.0.1:{server}"
    ep = endpoint.LoopbackEndpoint(base + "/v1")
    soon = time.monotonic() + 5
    assert ep.models(soon) == ["served-model"]
    with pytest.raises(endpoint.EndpointError, match="HTTP 302"):
        endpoint.LoopbackEndpoint(base + "/redirect").models(soon)
    with pytest.raises(endpoint.EndpointError, match="HTTP 500"):
        endpoint.LoopbackEndpoint(base + "/x").models(soon)
    with pytest.raises(endpoint.EndpointTimeout):
        ep.models(time.monotonic() - 1)


def test_status_parser_handles_renames_untracked_and_garbage():
    raw = (
        f"# branch.oid {BASE_SHA}\0# branch.head main\0"
        "2 R. N... 100644 100644 100644 a b R100 new name.py\0old.py\0? tests/t.py\0! x.log\0"
    ).encode()
    tree = gitstate.parse_status(raw)
    assert tree.observed and tree.head == BASE_SHA
    assert tree.paths() == ["new name.py", "old.py", "tests/t.py", "x.log"]
    assert not gitstate.parse_status(b"fatal: not a git repository\n").observed


@pytest.mark.parametrize(
    ("path", "test"),
    [
        ("tests/x.py", True),
        ("src/test_a.py", True),
        ("a_test.py", True),
        ("conftest.py", True),
        ("src/attestation.py", False),
        ("src/app.py", False),
    ],
)
def test_test_path_rule(path, test):
    assert episode.is_test_path(path) is test


def test_append_validates_before_writing(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    path = out / "episodes-x.jsonl"
    with pytest.raises(SchemaError):
        corpus_io.append_episode(path, {"episode_id": "e", "transcript": "text"}, out)
    assert not path.exists()


def test_validator_rejects_leaks_and_bad_arithmetic(harness):
    run, out = harness
    run(L0_REPLIES, "run")
    [rec] = lines(out)
    bad = json.loads(json.dumps(rec))
    bad["turns"][2]["tool_args"]["content"] = "raw text"
    with pytest.raises(SchemaError):
        validate(bad)
    bad = json.loads(json.dumps(rec))
    bad["tokens_total"]["prompt"] += 1
    with pytest.raises(SchemaError):
        validate(bad)
    bad = json.loads(json.dumps(rec))
    bad["transcript"] = []
    with pytest.raises(SchemaError):
        validate(bad)


LOOP_MODULES = ("agent.py", "endpoint.py", "episode.py", "episode_schema.py", "cli.py")
IO_NAMES = {"open", "read_text", "read_bytes", "write_text", "write_bytes", "listdir", "scandir"}
IO_NAMES |= {"walk", "iterdir", "glob", "rglob", "fdopen", "readlink", "mkdir", "unlink", "chmod"}


def _io_calls(tree: ast.AST) -> list[tuple[str, int]]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name in IO_NAMES:
                found.append((name, node.lineno))
    return found


def test_loop_modules_open_no_files():
    """Plan v2 point 1: only corpus_io (the recorder) touches files; the loop modules never do."""
    root = Path(episode.__file__).parent
    for name in LOOP_MODULES:
        tree = ast.parse((root / name).read_text())
        assert _io_calls(tree) == [], name
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not imported & {"subprocess", "shutil", "os", "glob", "io"}, name


def test_corpus_io_guards_every_path_it_opens():
    tree = ast.parse(Path(corpus_io.__file__).read_text())
    unguarded = {"load_task", "harness_version", "_wrapper_sha", "git"}  # fixed inputs, see doc
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        if fn.name in unguarded or not _io_calls(fn):
            continue
        calls = {getattr(n.func, "id", "") for n in ast.walk(fn) if isinstance(n, ast.Call)}
        assert "_inside" in calls, fn.name
