"""#88 local worker loop: fixes for the Codex review of #239 (REJECT, blocking items B1-B8)."""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any

import pytest
from local_worker_fakes import (
    META,
    TASK,
    Clock,
    World,
    cli_harness,
    lines,
    make_loop,
    reply,
)

from experimental.local_worker import agent, endpoint, episode, redact, tools
from experimental.local_worker.agent import initial_messages
from experimental.local_worker.episode_schema import SchemaError, validate_record

EDIT_SRC = reply("edit_file", path="src/app.py", target="x = 1", replacement="x = 2")
SECRET = "CONFIDENTIAL-model-text-4e1"


@pytest.fixture
def harness(tmp_path, monkeypatch):
    return cli_harness(tmp_path, monkeypatch)


def _record(tmp_path, replies, world=None, **limits):
    loop, world, endpoint, clock = make_loop(tmp_path, replies, world, **limits)
    out = loop.run(initial_messages(TASK))
    rec = episode.build_record(META, out)
    validate_record(rec)
    return rec, world


def raw_call(function: Any, call_id: Any = "c0") -> dict:
    msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": call_id, "function": function}],
    }
    return {
        "model": "served-model",
        "choices": [{"message": msg}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


# -- B1: no model text through arguments a tool does not take, or through a path never used ----
def test_an_unused_argument_is_a_recorded_protocol_abort(harness):
    run, out = harness
    finish = reply("finish", status="DONE", path=SECRET)
    assert run([EDIT_SRC, finish], "run") == 0
    [rec] = lines(out)
    assert rec["verification"]["exit_reason"] == "ERROR_ABORT"
    assert rec["termination"]["abort_cause"] == "protocol_error"
    assert rec["turns"][-1]["action_type"] == "invalid_tool_call"
    assert rec["turns"][-1]["error_category"] == "PROTOCOL_ERROR"
    assert rec["termination"]["cleanup"] == "ok" and rec["verification"]["patch_lines_added"] == 1
    assert SECRET not in json.dumps(rec)


def test_a_path_that_named_nothing_is_published_only_as_a_digest(tmp_path):
    rec, _ = _record(tmp_path, [reply("read_file", path=SECRET), reply("list_dir", path="src")])
    first, second = rec["turns"][0], rec["turns"][1]
    assert first["exit_code"] == 64 and redact.is_path_digest(first["tool_args"]["path"])
    assert second["tool_args"] == {"path": "src"}  # listed: a real directory
    assert SECRET not in json.dumps(rec)
    plain = {**first, "tool_args": {"path": SECRET}}
    with pytest.raises(SchemaError, match="successful call"):
        validate_record({**rec, "turns": [plain, *rec["turns"][1:]]})
    finish = {**rec["turns"][-1], "tool_args": {"status": "DONE", "path": "src"}}
    with pytest.raises(SchemaError, match="not a public argument"):
        validate_record({**rec, "turns": [*rec["turns"][:-1], finish]})


# -- B8: malformed calls are recorded aborts; the line and the cleanup always happen -----------
@pytest.mark.parametrize(
    "function",
    [
        {"name": [], "arguments": "{}"},
        {"name": {}, "arguments": "{}"},
        {"name": "read_file", "arguments": 7},
        "read_file",
    ],
)
def test_a_malformed_call_is_a_recorded_abort(harness, function):
    run, out = harness
    assert run([EDIT_SRC, raw_call(function)], "run") == 0
    [rec] = lines(out)
    assert rec["termination"]["abort_cause"] == "protocol_error"
    assert rec["termination"]["cleanup"] == "ok" and rec["verification"]["patch_lines_added"] == 1


def test_a_non_string_call_id_is_a_protocol_error(tmp_path):
    rec, _ = _record(
        tmp_path, [raw_call({"name": "finish", "arguments": '{"status": "DONE"}'}, [1])]
    )
    assert rec["termination"]["abort_cause"] == "protocol_error"


def test_an_unexpected_error_is_recorded_and_cleaned_up(tmp_path, monkeypatch):
    def boom(call: Any, n: int) -> Any:
        raise LookupError("unexpected")

    monkeypatch.setattr(agent, "decode_call", boom)
    rec, world = _record(tmp_path, [EDIT_SRC])
    assert rec["verification"]["exit_reason"] == "ERROR_ABORT"
    assert rec["termination"]["abort_cause"] == "internal_error"
    assert [c[0] for c in world.calls][-2:] == ["sweep", "numstat"]


# -- B4: one absolute deadline for every wrapper call, its kill grace and the sweep after it ---
class SlowWorld(World):
    """Every wrapper call answers as World does, but only after its whole timeout plus the whole
    kill grace (the worst case of tools.subprocess_invoker)."""

    def __call__(self, argv: list[str], stdin: bytes | None, timeout: float) -> Any:
        result = super().__call__(argv, stdin, timeout)
        assert self.clock is not None
        self.clock.t += timeout + tools.KILL_GRACE_SECONDS
        return result


def test_no_tool_or_sweep_runs_past_the_deadline(tmp_path):
    clock = Clock()
    world = SlowWorld(clock=clock)
    wt = tools.WorkerTools(tmp_path / ("0" * 32), tmp_path / "c.jsonl", invoker=world)
    wt.clock, wt.deadline = clock, clock() + 60
    with pytest.raises(tools.ToolDeadline):  # read took 40 s: no room for the write and its sweep
        wt.edit_file("src/app.py", "x = 1", "x = 2")
    assert world.files["src/app.py"] == "x = 1\n" and clock() <= wt.deadline
    wt.deadline = clock() + 60
    wt.run_tests(["-q"])  # pytest gets 30 s (+10 grace), its sweep 10 s (+10 grace)
    assert [c[0] for c in world.calls][-2:] == ["pytest", "sweep"] and clock() <= wt.deadline
    assert [c[2] for c in world.calls][-2:] == [30.0, tools.SWEEP_TIMEOUT_SECONDS]
    with pytest.raises(tools.ToolDeadline):
        wt.read_file("src/app.py")
    assert clock() <= wt.deadline
    quick = World(clock=clock)
    wt = tools.WorkerTools(tmp_path / ("1" * 32), tmp_path / "d.jsonl", invoker=quick)
    wt.clock, wt.deadline = clock, clock() + 200
    wt.run_tests(["-q"])  # a quick run: its sweep still gets only its own short timeout
    assert [c[2] for c in quick.calls] == [170.0, tools.SWEEP_TIMEOUT_SECONDS]


def test_an_episode_of_worst_case_calls_ends_within_max_wall_seconds(tmp_path):
    replies = [EDIT_SRC, reply("run_tests"), reply("read_file", path="src/app.py")] * 5
    loop, world, _, clock = make_loop(
        tmp_path,
        replies,
        SlowWorld(),
        max_wall_seconds=400,
        cleanup_reserve_seconds=60,
        tool_cap_seconds=30,
    )
    start = clock()
    out = loop.run(initial_messages(TASK))
    assert out.exit_reason == "TIMEOUT" and out.cleanup == "ok"
    assert len(out.turns) >= 3 and clock() - start <= 400
    assert clock() - start > 340  # it did use the time: the bound is not vacuous


# -- B3: the endpoint's deadline holds through headers and body ----------------------------------
def _trickle_server(head: bytes, trickle: bytes, size: int = 8, every: float = 0.08) -> int:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def serve() -> None:
        conn, _ = srv.accept()
        conn.recv(65536)
        try:
            conn.sendall(head)
            for i in range(0, len(trickle), size):
                conn.sendall(trickle[i : i + size])
                time.sleep(every)
        except OSError:
            pass
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1]


@pytest.mark.parametrize(
    ("head", "trickle"),
    [
        (b"HTTP/1.1 200 OK\r\n", b"X-Slow: " + b"a" * 200 + b"\r\n\r\n{}"),  # headers trickle
        (b"HTTP/1.1 200 OK\r\nContent-Length: 200\r\n\r\n", b"{" * 200),  # body trickles
    ],
)
def test_the_codex_trickle_repro_stops_at_the_deadline(head, trickle):
    """8 bytes every 80 ms against a 150 ms budget (the review's repro ran 686 ms)."""
    port = _trickle_server(head, trickle)
    started = time.monotonic()
    with pytest.raises(endpoint.EndpointTimeout):
        endpoint.LoopbackEndpoint(f"http://127.0.0.1:{port}/v1").models(started + 0.15)
    assert time.monotonic() - started < 0.4


def test_a_reply_read_after_the_deadline_is_a_timeout(monkeypatch):
    """The clock is read once more after the body: a reply complete only past the deadline is
    never parsed. The fake clock jumps past the deadline as soon as the body has been read."""
    body = b'{"data": [{"id": "m"}]}'
    head = b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body
    read = endpoint._read_bounded
    done = threading.Event()

    def read_then_late(*args: Any) -> bytes:
        data = read(*args)
        done.set()
        return data

    monkeypatch.setattr(endpoint, "_read_bounded", read_then_late)
    port = _trickle_server(head, b"")
    late = endpoint.LoopbackEndpoint(
        f"http://127.0.0.1:{port}/v1",
        clock=lambda: time.monotonic() + (100 if done.is_set() else 0),
    )
    with pytest.raises(endpoint.EndpointTimeout):
        late.models(time.monotonic() + 5)


# -- B7: a tree that was never observed is an indeterminate cut, never "no edit" -----------------
class BlindWorld(World):
    """Status answers for the base, then fails ``blind`` times (exit 3), then answers again."""

    def __init__(self, blind: int, **kw: Any) -> None:
        super().__init__(**kw)
        self.blind, self.statuses = blind, 0

    def handle(self, action: str, args: list[str], stdin: bytes) -> tuple[int | None, bytes]:
        if action == "status":
            self.statuses += 1
            if 1 < self.statuses <= 1 + self.blind:
                return 3, b"fatal: could not open directory\n"
        return super().handle(action, args, stdin)


def test_an_edit_never_observed_is_an_unobserved_cut(tmp_path):
    rec, world = _record(tmp_path, [EDIT_SRC], BlindWorld(blind=2))
    edit, source = rec["first_edit_cut"], rec["first_source_edit"]
    assert rec["termination"]["abort_cause"] == "status_unavailable"
    assert (edit["cut_status"], edit["cut_occurred"]) == ("UNOBSERVED", None)
    assert (source["cut_status"], source["occurred"]) == ("UNOBSERVED", None)
    assert rec["turns"][0]["tool_ran"] and rec["turns"][0]["tree_differs_from_base"] is None
    assert rec["verification"]["patch_lines_added"] == 1  # the edit happened; the cut is unknown
    assert rec["cut_900s"]["tree_differs_at_limit"] is None
    no_edit = {**edit, "cut_occurred": False, "cut_status": "NO_CUT"}
    no_source = {**source, "occurred": False, "cut_status": "NO_CUT"}
    with pytest.raises(SchemaError):
        validate_record({**rec, "first_edit_cut": no_edit, "first_source_edit": no_source})
    with pytest.raises(SchemaError):
        validate_record({**rec, "first_edit_cut": {**edit, "cut_occurred": False}})
    with pytest.raises(SchemaError, match="never observed"):
        validate_record({**rec, "first_source_edit": no_source})


def test_a_run_stopped_before_its_status_is_observed_in_cleanup(tmp_path):
    rec, world = _record(tmp_path, [EDIT_SRC], BlindWorld(blind=1))
    edit = rec["first_edit_cut"]
    assert rec["termination"]["abort_cause"] == "status_unavailable"
    assert (edit["cut_status"], edit["cut_turn_index"], edit["target_file"]) == (
        "CUT",
        0,
        "src/app.py",
    )
    assert [c[0] for c in world.calls][-3:] == ["sweep", "numstat", "status"]


def test_an_unobserved_parent_is_no_clean_prefix_for_resume(harness, monkeypatch):
    run, out = harness
    monkeypatch.setattr("local_worker_fakes.World", lambda: BlindWorld(blind=2))
    assert run([EDIT_SRC], "run") == 0
    monkeypatch.setattr("local_worker_fakes.World", World)  # not undo(): the harness stays
    [parent] = lines(out)
    assert parent["first_edit_cut"]["cut_status"] == "UNOBSERVED"
    tr = str(out / "private" / parent["episode_id"] / "transcript.json")
    done = [reply("finish", status="DONE")]
    assert run(done, "resume", "--parent-transcript", tr, "--cut-turn", "0") == 2
    assert len(lines(out)) == 1
    run([reply("read_file", path="src/app.py"), EDIT_SRC], "run")  # the same harness resumes
    tr = str(out / "private" / lines(out)[1]["episode_id"] / "transcript.json")
    assert run(done, "resume", "--parent-transcript", tr) == 0


def test_a_turn_whose_tool_never_ran_has_no_tree(tmp_path):
    rec, _ = _record(tmp_path, [reply("read_file", path="src/app.py")])
    turn = {**rec["turns"][-1]}  # finish: no tool ran
    assert turn["tool_ran"] is False and turn["tree_differs_from_base"] is None
    with pytest.raises(SchemaError):
        validate_record(
            {**rec, "turns": [*rec["turns"][:-1], {**turn, "tree_differs_from_base": False}]}
        )


class DeadlineWorld(BlindWorld):
    """The write times out (11 s) and uses the whole kill grace, ending at 1120; every status,
    sweep and numstat after the base uses its timeout plus its grace (20 s)."""

    def handle(self, action: str, args: list[str], stdin: bytes) -> tuple[int | None, bytes]:
        assert self.clock is not None
        if action == "write":  # the file is written, but the call reports a timeout
            self.clock.t += 11 + tools.KILL_GRACE_SECONDS
            return None, super().handle(action, args, stdin)[1]
        if action in ("status", "sweep", "numstat") and self.clock() > 1001:
            self.clock.t += 20  # sweep after the write to 1140, then 1160, 1180, 1200
        return super().handle(action, args, stdin)


def test_the_late_status_never_runs_past_max_wall_seconds(tmp_path):
    """The edit's write ends at the loop deadline; the failed status, the sweep and numstat
    each use their whole third of the reserve, so the late status has no time left."""
    loop, world, _, clock = make_loop(
        tmp_path,
        [EDIT_SRC],
        DeadlineWorld(blind=1),
        max_wall_seconds=200,
        cleanup_reserve_seconds=60,
        model_seconds=99,
    )
    start = clock()
    out = loop.run(initial_messages(TASK))
    assert 195 < clock() - start <= 200
    rec = episode.build_record(META, out)
    validate_record(rec)
    assert rec["first_edit_cut"]["cut_status"] == "UNOBSERVED"


# -- B6: a resume continues the same task, configuration and base in a fresh run root ------------
def _parent(run, out) -> str:
    assert run([reply("read_file", path="src/app.py"), EDIT_SRC], "run", root="a" * 32) == 0
    return str(out / "private" / lines(out)[0]["episode_id"] / "transcript.json")


@pytest.mark.parametrize(
    "change",
    [
        {"prompt": "another prompt"},
        {"repo": "repo_y"},
        {"public_tests": ["tests/test_other.py"]},
        {"task_id": "TASK_02"},
    ],
)
def test_resume_refuses_another_task_binding(harness, tmp_path, change):
    run, out = harness
    tr = _parent(run, out)
    task = json.loads((tmp_path / "task.json").read_text())
    other = tmp_path / "other-task.json"
    other.write_text(json.dumps({**task, **change}))
    done = [reply("finish", status="DONE")]
    assert run(done, "resume", "--parent-transcript", tr, "--task", str(other)) == 2
    assert run(done, "resume", "--parent-transcript", tr) == 0  # the same task resumes


def test_resume_refuses_another_model_configuration(harness):
    run, out = harness
    tr = _parent(run, out)
    (out / "manifest.json").unlink()  # a fresh manifest, now with another temperature
    done = [reply("finish", status="DONE")]
    assert run(done, "resume", "--parent-transcript", tr, "--temperature", "0.7") == 2


def test_a_reused_run_root_keeps_its_checkout(harness):
    """The harness keeps one fake checkout per run root, as on disk: what the parent wrote is
    still there, so a resume in its root is refused (reused root, and a dirty tree)."""
    run, out = harness
    tr = _parent(run, out)
    done = [reply("finish", status="DONE")]
    assert run(done, "resume", "--parent-transcript", tr, root="a" * 32) == 2
    assert run(done, "run", "--seed", "5", root="a" * 32) == 2  # its tree is not clean at base
    assert len(lines(out)) == 1


def test_the_parents_root_is_refused_even_once_its_corpus_file_is_archived(harness, capsys):
    run, out = harness
    tr = _parent(run, out)
    for corpus in out.glob("episodes-*.jsonl"):
        corpus.rename(out.parent / corpus.name)  # archived: no line names root a* any more
    capsys.readouterr()
    done = [reply("finish", status="DONE")]
    assert run(done, "resume", "--parent-transcript", tr, root="a" * 32) == 2
    assert "needs a fresh run root" in capsys.readouterr().err
