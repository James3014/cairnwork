"""A capped capture is never parsed as complete output (Claude review of #239, NB5).

subprocess_invoker keeps at most tools.CAPTURE_LIMIT bytes of a stream whole; a longer stream
comes back as head + marker + tail, longer than the limit. A git query whose output was capped is
not observed (status: the tree; numstat: the patch counts), and edit_file never writes back a
file it could not read whole.
"""

import pytest
from local_worker_fakes import TASK, World, make_loop, reply

from experimental.local_worker import tools
from experimental.local_worker.agent import RefusedStart, initial_messages

LIMIT = 200


class PaddedWorld(World):
    """Status output that parses (comment fields pad it) but is longer than the capture limit
    once ``pad`` is set: a parser that ignored the cap would still see a complete tree."""

    pad = False

    def status(self) -> bytes:
        out = super().status()
        return out + b"# pad\0" * (LIMIT // 6 + 1) if self.pad else out


@pytest.fixture
def small_limit(monkeypatch):
    monkeypatch.setattr(tools, "CAPTURE_LIMIT", LIMIT)


def test_subprocess_invoker_caps_a_long_stream_beyond_the_limit(small_limit):
    invoke = tools.subprocess_invoker()
    rc, out, _ = invoke(["/bin/sh", "-c", f"printf '%0{LIMIT + 1}d' 0"], None, 30)
    assert rc == 0 and len(out) > LIMIT and b"not captured" in out
    rc, out, _ = invoke(["/bin/sh", "-c", f"printf '%0{LIMIT}d' 0"], None, 30)
    assert rc == 0 and len(out) == LIMIT  # read whole: at most the limit


def test_a_capped_base_status_refuses_the_start(tmp_path, small_limit):
    world = PaddedWorld()
    world.pad = True
    loop, _, _, _ = make_loop(tmp_path, [], world=world)
    with pytest.raises(RefusedStart):
        loop.run(initial_messages(TASK))


def test_a_capped_status_after_a_tool_is_not_observed(tmp_path, small_limit):
    world = PaddedWorld()
    write = reply("write_file", path="src/app.py", content="x = 2\n")
    loop, _, _, _ = make_loop(tmp_path, [write], world=world)
    real_handle = world.handle

    def handle(action, args, stdin):
        if action == "write":
            world.pad = True  # every status from here on is capped
        return real_handle(action, args, stdin)

    world.handle = handle
    out = loop.run(initial_messages(TASK))
    assert out.exit_reason == "ERROR_ABORT" and out.abort_cause == "status_unavailable"
    assert out.turns[0].tree is not None and not out.turns[0].tree.observed


def test_a_capped_numstat_is_no_patch_count(tmp_path, monkeypatch):
    world = World()
    real = world.__call__

    def invoker(argv, stdin, timeout):
        if "numstat" in argv:  # its one PATCH line is now longer than the cap: a capped capture
            monkeypatch.setattr(tools, "CAPTURE_LIMIT", 5)
        return real(argv, stdin, timeout)

    loop, _, _, _ = make_loop(tmp_path, [], world=world)
    loop.tools.invoker = invoker
    out = loop.run(initial_messages(TASK))
    assert out.patch is None and out.cleanup == "failed"


def test_edit_file_never_writes_back_a_capped_read(tmp_path, small_limit):
    big = "x = 1\n" + "# filler\n" * LIMIT
    world = World({"src/app.py": big, "tests/test_app.py": "def test(): 0\n"})
    loop, world, _, _ = make_loop(tmp_path, [], world=world)
    call = loop.tools.edit_file("src/app.py", "x = 1", "x = 2")
    assert call.exit_code == 1 and "too large" in call.output
    assert world.files["src/app.py"] == big
    assert [c[0] for c in world.calls] == ["read"]  # no write was started


def test_a_capped_base_listing_publishes_no_plain_path(tmp_path, monkeypatch):
    """A listing that is longer than the cap may still end on a NUL and parse; it is not used."""
    world = World()
    real = world.__call__

    def invoker(argv, stdin, timeout):
        monkeypatch.setattr(tools, "CAPTURE_LIMIT", 5 if "tracked" in argv else LIMIT * 100)
        return real(argv, stdin, timeout)

    loop, _, _, _ = make_loop(tmp_path, [reply("read_file", path="src/app.py")], world=world)
    loop.tools.invoker = invoker
    out = loop.run(initial_messages(TASK))
    assert out.base_paths is None and out.cleanup == "ok"
    assert out.turns[0].exit_code == 0  # the read itself was whole
