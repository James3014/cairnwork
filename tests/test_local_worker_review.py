"""#88 local worker loop: fixes from the independent review of #239 (findings 1-10)."""

from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
from typing import Any

import pytest
from local_worker_fakes import META, TASK, World, cli_harness, lines, make_loop, reply

from experimental.local_worker import endpoint, episode, redact, tools
from experimental.local_worker.agent import Limits, RefusedStart, initial_messages
from experimental.local_worker.episode_schema import SchemaError, validate_record

EDIT_SRC = reply("edit_file", path="src/app.py", target="x = 1", replacement="x = 2")


@pytest.fixture
def harness(tmp_path, monkeypatch):
    return cli_harness(tmp_path, monkeypatch)


def transcript_of(out, episode_id):
    return out / "private" / episode_id / "transcript.json"


def test_resume_from_a_turn_3_cut_uses_one_basis(harness):
    """Finding 1: the child's cut fields and times are its own; the parent's prefix is only in
    parent_pre_cut_*; wall_clock_seconds = parent pre-cut seconds + own seconds."""
    run, out = harness
    parent_replies = [
        reply("read_file", usage=(100, 10, 50), path="src/app.py"),
        reply("list_dir", usage=(200, 20, 100), path="."),
        reply("read_file", usage="missing", path="src/app.py"),
        EDIT_SRC,  # turn 3: the cut
        reply("finish", status="DONE"),
    ]
    assert run(parent_replies, "run") == 0
    [parent] = lines(out)
    assert parent["first_edit_cut"]["cut_turn_index"] == 3
    child_replies = [
        reply("read_file", path="src/app.py"),
        EDIT_SRC,
        reply("finish", status="DONE"),
    ]
    tr = str(transcript_of(out, parent["episode_id"]))
    assert run(child_replies, "resume", "--seed", "1000", "--parent-transcript", tr) == 0
    child = lines(out)[1]
    assert child["parent_pre_cut_read_actions"] == 3
    assert child["parent_pre_cut_tokens_accum"] is None  # turn 2 had no usage
    assert child["parent_pre_cut_turns_missing_usage"] == 1
    assert child["parent_pre_cut_wall_seconds"] == parent["first_edit_cut"]["pre_cut_wall_seconds"]
    cut = child["first_edit_cut"]
    assert cut["cut_turn_index"] == 4  # child turns are numbered on from the cut (3, 4, 5)
    assert cut["pre_cut_read_actions"] == 1  # only the child's own read
    assert cut["pre_cut_tokens_accum"] == 160 and cut["pre_cut_turns_missing_usage"] == 0
    assert child["turns"][0]["turn_index"] == 3 and child["turns"][0]["t_start_seconds"] < 0.5
    assert child["wall_clock_seconds"] == pytest.approx(
        child["parent_pre_cut_wall_seconds"] + child["own_wall_seconds"], abs=0.002
    )
    assert parent["parent_pre_cut_wall_seconds"] is None  # a fresh run has no parent fields


def test_resume_refuses_a_resume_parent(harness):
    run, out = harness
    run([reply("read_file", path="src/app.py"), EDIT_SRC], "run")
    [parent] = lines(out)
    tr = str(transcript_of(out, parent["episode_id"]))
    assert run([reply("finish", status="DONE")], "resume", "--parent-transcript", tr) == 0
    child = lines(out)[1]
    tr2 = str(transcript_of(out, child["episode_id"]))
    assert run([], "resume", "--parent-transcript", tr2, "--cut-turn", "1") == 2


def test_cut_900s_budget_shrinks_by_the_parent_prefix():
    turns = [{"t_end_seconds": 50.0, "tree_differs_from_base": True}]
    got = episode.cut_at(turns, "COMPLETED", 60.0, max(0.0, 900.0 - 870.0))
    assert got["limit_seconds"] == 30.0 and got["within_limit"] is False
    assert got["exit_reason"] == "TIMEOUT" and got["turns_completed"] == 0


# -- finding 5: no overrun of the deadline -----------------------------------------------------
def test_reserve_must_hold_three_steps_with_their_kill_grace():
    with pytest.raises(ValueError):
        Limits(context_cap=1000, cleanup_reserve_seconds=20)
    lim = Limits(context_cap=1000)
    assert 3 * (lim.step_seconds + tools.KILL_GRACE_SECONDS) == lim.cleanup_reserve_seconds


def test_edit_file_read_and_write_share_one_timeout(tmp_path):
    seen = []

    def invoker(argv, stdin, timeout):
        seen.append(timeout)
        nonce = argv[argv.index("--start-nonce") + 1]
        if argv[argv.index("--start-nonce") + 2] == "read":
            time.sleep(0.3)
            return 0, b"x = 1\n", f"{tools.START_MARKER} {nonce}\n".encode()
        return 0, b"", f"{tools.START_MARKER} {nonce}\n".encode()

    wt = tools.WorkerTools(tmp_path / ("0" * 32), tmp_path / "c.jsonl", invoker=invoker)
    wt.timeout_seconds = 1.0
    assert wt.edit_file("a.py", "x = 1", "x = 2").exit_code == 0
    assert seen[0] == 1.0 and seen[1] <= 0.71


def _drip_server(head: bytes, drip: bytes):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        conn.recv(65536)
        conn.sendall(head)
        try:
            for b in drip:
                conn.sendall(bytes([b]))
                time.sleep(0.1)
        except OSError:
            pass
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1]


@pytest.mark.parametrize(
    ("head", "drip"),
    [
        (b"HTTP/1.1 200 OK\r\nContent-Length: 400\r\n\r\n", b"{" * 400),  # body drip
        (b"HTTP/1.1 200 OK\r\n", b"X-Slow: " + b"a" * 400),  # header drip
        (b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n", b"{" * 400),  # no length: until close
    ],
)
def test_a_dripping_server_cannot_pass_the_deadline(head, drip):
    port = _drip_server(head, drip)
    ep = endpoint.LoopbackEndpoint(f"http://127.0.0.1:{port}/v1")
    started = time.monotonic()
    with pytest.raises(endpoint.EndpointTimeout):
        ep.models(started + 0.8)
    assert time.monotonic() - started < 1.5  # each byte comes well inside the socket timeout


def test_body_reads_are_single_receives_checked_against_the_deadline():
    """Without the watchdog: each read1 is one receive, and the deadline is checked between them."""

    class Drip:
        def __init__(self) -> None:
            self.reads = 0

        def isclosed(self) -> bool:
            return False

        def read1(self, n: int) -> bytes:
            self.reads += 1
            return b"{"

    class Sock:
        def settimeout(self, t: float) -> None:
            pass

    left = iter([5.0, 4.0, 3.0])

    def remaining() -> float:
        try:
            return next(left)
        except StopIteration:
            raise endpoint.EndpointTimeout("deadline passed") from None

    drip = Drip()
    with pytest.raises(endpoint.EndpointTimeout):
        endpoint._read_bounded(drip, Sock(), remaining)  # type: ignore[arg-type]
    assert drip.reads == 3


def test_status_with_a_nonzero_exit_is_not_observed(tmp_path):
    """Review of #239, finding 3: a failed git query is never parsed, even if its output looks
    like a clean tree (base: refuse to start; after a tool call: abort)."""
    world = World()
    world.status_rc = 3
    loop, _, _, _ = make_loop(tmp_path, [EDIT_SRC], world)
    with pytest.raises(RefusedStart):
        loop.run(initial_messages(TASK))
    world = World()
    loop, world, _, _ = make_loop(tmp_path / "b", [reply("read_file", path="src/app.py")], world)
    orig = world.handle

    def handle(action, args, stdin):
        rc, data = orig(action, args, stdin)
        after_read = len(world.calls) > 1 and world.calls[-2][0] == "read"
        return (4 if action == "status" and after_read else rc), data

    world.handle = handle
    out = loop.run(initial_messages(TASK))
    assert out.exit_reason == "ERROR_ABORT" and out.abort_cause == "status_unavailable"


# -- extra mutants from the review: M4 (_inside as a prefix check), M5 (peer check removed) ------
def test_inside_is_a_path_check_not_a_string_prefix(tmp_path):
    from experimental.local_worker import corpus_io

    (tmp_path / "out").mkdir()
    (tmp_path / "out2").mkdir()
    (tmp_path / "out2" / "canary.json").write_text("{}")
    assert (
        corpus_io._inside(tmp_path / "out" / "x", tmp_path / "out")
        == (tmp_path / "out" / "x").resolve()
    )
    with pytest.raises(corpus_io.InputRefused):
        corpus_io._inside(tmp_path / "out2" / "canary.json", tmp_path / "out")


def test_request_checks_the_connected_peer(monkeypatch):
    body = b'{"data": [{"id": "m"}]}'
    head = b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body
    port = _drip_server(head, b"")
    ep = endpoint.LoopbackEndpoint(f"http://127.0.0.1:{port}/v1")
    monkeypatch.setattr(endpoint, "peer_is_loopback", lambda peer: False)
    with pytest.raises(endpoint.EndpointError, match="not a loopback") as err:
        ep.models(time.monotonic() + 5)
    assert type(err.value) is endpoint.EndpointError
    monkeypatch.undo()
    port = _drip_server(head, b"")
    assert endpoint.LoopbackEndpoint(f"http://127.0.0.1:{port}/v1").models(
        time.monotonic() + 5
    ) == ["m"]


def test_resume_refuses_a_reused_run_root(harness):
    """Finding 8: a resume must start in a fresh run root, never the parent's or another one
    already in the corpus (leftover <run_root>/tmp files would carry over)."""
    run, out = harness
    parent_root, other_root = "a" * 32, "b" * 32
    assert run([reply("read_file", path="src/app.py"), EDIT_SRC], "run", root=parent_root) == 0
    assert run([reply("finish", status="DONE")], "run", "--seed", "1", root=other_root) == 0
    tr = str(transcript_of(out, lines(out)[0]["episode_id"]))
    done = [reply("finish", status="DONE")]
    assert run(done, "resume", "--parent-transcript", tr, root=parent_root) == 2
    assert run(done, "resume", "--parent-transcript", tr, root=other_root) == 2
    assert len(lines(out)) == 2
    assert run(done, "resume", "--parent-transcript", tr, root="c" * 32) == 0
    assert lines(out)[2]["checkout_id"] == "c" * 32


def test_resume_refuses_the_parent_run_root_even_without_its_line(harness):
    run, out = harness
    assert run([reply("read_file", path="src/app.py"), EDIT_SRC], "run", root="d" * 32) == 0
    tr = str(transcript_of(out, lines(out)[0]["episode_id"]))
    for f in out.glob("episodes-*.jsonl"):
        f.unlink()  # the parent's checkout_id is then known only from its transcript
    assert (
        run([reply("finish", status="DONE")], "resume", "--parent-transcript", tr, root="d" * 32)
        == 2
    )


# -- finding 9: bounded paths -------------------------------------------------------------------
def _record(tmp_path, replies, world=None):
    loop, world, _, _ = make_loop(tmp_path, replies, world)
    rec = episode.build_record(META, loop.run(initial_messages(TASK)))
    validate_record(rec)
    return rec


def test_a_long_path_is_kept_only_as_a_digest(tmp_path):
    long = "src/" + "d" * 600 + ".py"
    rec = _record(tmp_path, [reply("write_file", path=long, content="x")])
    want = {"sha256": hashlib.sha256(long.encode()).hexdigest(), "bytes": len(long)}
    cut, source = rec["first_edit_cut"], rec["first_source_edit"]
    assert rec["turns"][0]["tool_args"]["path"] == want
    assert cut["target_file"] == want and cut["changed_paths"] == [want]
    assert source["path"] == want and long not in json.dumps(rec)
    for bad in ({**cut, "target_file": long}, {**cut, "changed_paths": [long]}):
        with pytest.raises(SchemaError):
            validate_record({**rec, "first_edit_cut": bad})
    turn = {**rec["turns"][0], "tool_args": {"path": long}}
    with pytest.raises(SchemaError):
        validate_record({**rec, "turns": [turn, *rec["turns"][1:]]})


def test_changed_paths_are_capped_with_the_full_count(tmp_path):
    world = World()
    world.pytest_writes = {f"build/out{n:03}.txt": "x" for n in range(70)}
    rec = _record(tmp_path, [reply("run_tests")], world)
    cut = rec["first_edit_cut"]
    assert cut["changed_paths_total"] == 70
    assert cut["changed_paths"] == [f"build/out{n:03}.txt" for n in range(redact.MAX_CHANGED_PATHS)]
    for bad in (
        {**cut, "changed_paths_total": 63},  # 64 entries cannot be the full list of 63
        {**cut, "changed_paths": cut["changed_paths"][:10]},
    ):
        with pytest.raises(SchemaError):
            validate_record({**rec, "first_edit_cut": bad})


def test_a_body_cut_off_before_its_length_is_an_error_not_json():
    """A server that closes before Content-Length is reached sends a truncated body: the
    endpoint reports the cut-off read, before the deadline, as an error (not a parse of half)."""
    port = _drip_server(b"HTTP/1.1 200 OK\r\nContent-Length: 40\r\n\r\n" + b'{"data": []}', b"")
    with pytest.raises(endpoint.EndpointError, match="IncompleteRead"):
        endpoint.LoopbackEndpoint(f"http://127.0.0.1:{port}/v1").models(time.monotonic() + 5)


def test_a_read_ended_by_the_watchdog_is_a_timeout(monkeypatch):
    """Whichever wins at the deadline, the socket timeout or the watchdog's shutdown (which
    reads as end of body), the result is TIMEOUT. Here the watchdog is made to win."""
    real_timer = threading.Timer
    monkeypatch.setattr(endpoint.threading, "Timer", lambda t, fn, args: real_timer(0.3, fn, args))
    port = _drip_server(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n", b"{" * 400)
    started = time.monotonic()
    with pytest.raises(endpoint.EndpointTimeout):
        endpoint.LoopbackEndpoint(f"http://127.0.0.1:{port}/v1").models(started + 5)
    assert time.monotonic() - started < 1.5


# -- finding 10: cut definition in the manifest; pre-cut values checked against the turns ------
def test_manifest_states_the_cut_definition(harness):
    run, out = harness
    run([reply("read_file", path="src/app.py"), EDIT_SRC], "run")
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["cut_definition"] == episode.CUT_DEFINITION
    assert (
        "§3/§8" in manifest["cut_definition"] and "first_source_edit" in manifest["cut_definition"]
    )


@pytest.mark.parametrize(
    ("cut", "key", "value"),
    [
        ("first_edit_cut", "pre_cut_read_actions", 2),
        ("first_edit_cut", "pre_cut_tokens_accum", 999),
        ("first_edit_cut", "pre_cut_wall_seconds", 0.5),
        ("first_edit_cut", "cut_turn_index", 2),
        ("first_source_edit", "pre_read_actions", 2),
        ("first_source_edit", "pre_tokens_accum", 999),
        ("first_source_edit", "pre_wall_seconds", 0.5),
        ("first_source_edit", "turn_index", 9),
    ],
)
def test_validator_recomputes_pre_cut_values_from_the_turns(tmp_path, cut, key, value):
    replies = [
        reply("read_file", path="src/app.py"),
        reply("write_file", path="tests/test_new.py", content="x"),  # turn 1: the tree cut
        EDIT_SRC,  # turn 2: the source cut
    ]
    rec = _record(tmp_path, replies)
    assert (
        rec["first_edit_cut"]["cut_turn_index"] == 1 and rec["first_source_edit"]["turn_index"] == 2
    )
    with pytest.raises(SchemaError):
        validate_record({**rec, cut: {**rec[cut], key: value}})


def test_validator_refuses_a_missing_cut_when_the_tree_changed(tmp_path):
    rec = _record(tmp_path, [EDIT_SRC])
    no_cut = {"cut_occurred": False, **dict.fromkeys(episode.CUT_FIELDS[1:])}
    no_source = {"occurred": False, **dict.fromkeys(episode.SOURCE_FIELDS[1:])}
    with pytest.raises(SchemaError):
        validate_record({**rec, "first_edit_cut": no_cut, "first_source_edit": no_source})


def test_validator_ties_the_edit_cut_to_the_first_changed_tree(tmp_path):
    from experimental.local_worker import episode_schema

    replies = [reply("write_file", path="tests/test_new.py", content="x"), EDIT_SRC]
    rec = _record(tmp_path, replies)
    reads, tokens, wall, missing = episode_schema._pre_cut(rec["turns"], 1)
    late: dict[str, Any] = dict(rec["first_edit_cut"], cut_turn_index=1)
    late.update(pre_cut_read_actions=reads, pre_cut_tokens_accum=tokens)
    late.update(pre_cut_wall_seconds=wall, pre_cut_turns_missing_usage=missing)
    # self-consistent, but turn 0 changed the tree first
    with pytest.raises(SchemaError, match="first turn whose tree differs"):
        validate_record({**rec, "first_edit_cut": late})
