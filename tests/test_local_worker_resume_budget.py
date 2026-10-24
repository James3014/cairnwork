"""A resume's wall budget is what the parent left (#88 follow-up): max_wall_seconds minus
parent_pre_cut_wall_seconds, refused at start when that leaves no more than the cleanup reserve
(zero or below included)."""

import json

import pytest
from local_worker_fakes import BASE_SHA, TASK, cli_harness, lines, make_loop, reply

from experimental.local_worker import cli
from experimental.local_worker.agent import RefusedStart, initial_messages

READ = reply("read_file", path="src/app.py")


def _resume(tmp_path, offset, **limits):
    loop, world, endpoint, clock = make_loop(tmp_path, [READ] * 50, **limits)
    start = clock.t
    out = loop.run(initial_messages(TASK), first_turn=3, elapsed_offset=offset)
    return out, world, endpoint, clock.t - start


def test_the_resume_deadline_is_what_the_parent_left(tmp_path):
    lim = dict(max_wall_seconds=1000.0, cleanup_reserve_seconds=60.0, model_seconds=100.0)
    out, _, endpoint, spent = _resume(tmp_path, 700.0, **lim)
    assert endpoint.deadlines[0] == pytest.approx(
        1000.0 + 1000 - 700 - 60
    )  # the clock starts at 1000
    assert out.exit_reason == "TIMEOUT" and out.limit_hit == "max_wall_seconds"
    assert [t.index for t in out.turns] == [3, 4]  # 0-100, 100-200; the third would pass 240
    assert spent <= 1000 - 700 and out.wall_seconds <= 1000 - 700
    fresh, _, fresh_endpoint, _ = _resume(tmp_path / "fresh", 0.0, **lim)
    assert fresh_endpoint.deadlines[0] == pytest.approx(1000.0 + 1000 - 60)
    assert len(fresh.turns) == 9


@pytest.mark.parametrize("offset", [940.0, 999.0, 1000.0, 1000.5, 5000.0])
def test_no_budget_left_refuses_the_resume_before_anything_runs(tmp_path, offset):
    loop, world, endpoint, _ = make_loop(tmp_path, [READ], max_wall_seconds=1000.0)
    with pytest.raises(RefusedStart, match="wall"):
        loop.run(initial_messages(TASK), first_turn=3, elapsed_offset=offset)
    assert world.calls == [] and endpoint.payloads == []


def test_a_budget_just_above_the_reserve_starts(tmp_path):
    loop, _, endpoint, _ = make_loop(tmp_path, [], max_wall_seconds=1000.0, model_seconds=0.5)
    out = loop.run(initial_messages(TASK), first_turn=3, elapsed_offset=939.0)
    assert endpoint.deadlines == [pytest.approx(1001.0)]  # 61 s left, 60 of them the reserve
    assert out.exit_reason == "COMPLETED" and [t.index for t in out.turns] == [3]


# -- the CLI: the budget comes from the parent's recorded parent_pre_cut_wall_seconds -----------
@pytest.fixture
def harness(tmp_path, monkeypatch):
    return cli_harness(tmp_path, monkeypatch)


def _parent(run, out, cut_start: float) -> str:
    """A parent whose cut turn (1) started ``cut_start`` seconds into it."""
    edit = reply("edit_file", path="src/app.py", target="x", replacement="y")
    assert run([READ, edit], "run") == 0
    path = out / "private" / lines(out)[0]["episode_id"] / "transcript.json"
    data = json.loads(path.read_text())
    for t in data["turns"]:
        t["t_start"], t["t_end"] = (cut_start, cut_start) if t["index"] >= 1 else (0.0, 1.0)
    path.write_text(json.dumps(data))
    assert cli.resume_prefix(data, None)[2] == cut_start
    return str(path)


@pytest.mark.parametrize("cut_start", [1800.0, 1740.0, 2500.0])  # default max_wall 1800, reserve 60
def test_cli_refuses_a_resume_with_no_budget_left(harness, capsys, cut_start):
    run, out = harness
    tr = _parent(run, out, cut_start)
    capsys.readouterr()
    assert run([], "resume", "--parent-transcript", tr) == 2
    assert "no wall time left" in capsys.readouterr().err
    assert len(lines(out)) == 1 and len(list((out / "private").iterdir())) == 1


def test_cli_resume_records_the_parents_seconds_and_runs_within_the_rest(harness, monkeypatch):
    run, out = harness
    tr = _parent(run, out, 1200.0)
    seen = []
    real = cli.AgentLoop.run

    def spy(self, messages, **kw):
        seen.append((kw["elapsed_offset"], self.limits.max_wall_seconds))
        return real(self, messages, **kw)

    monkeypatch.setattr(cli.AgentLoop, "run", spy)
    assert run([reply("finish", status="DONE")], "resume", "--parent-transcript", tr) == 0
    line = lines(out)[1]
    assert line["parent_pre_cut_wall_seconds"] == 1200.0
    assert line["wall_clock_seconds"] == pytest.approx(1200.0 + line["own_wall_seconds"])
    assert BASE_SHA == line["base_sha"]
    assert seen == [(1200.0, 1800.0)]  # the loop's clock starts 1200 s in: 600 s are left
