"""A planted ignore file cannot hide new source files from first_source_edit (Claude review of
#239, NB3). A src/.gitignore containing "*" ignores itself and everything new beside it; a new or
changed ignore file is therefore itself a source change, in either order of planting."""

import subprocess

import pytest
from local_worker_fakes import BASE_SHA, TAG, TASK, World, make_loop, make_runs, reply
from local_worker_fakes import run_wrapper as run

from experimental.local_worker.agent import initial_messages
from experimental.local_worker.cuts import first_source_edit, is_ignore_file
from experimental.local_worker.episode import TurnLog
from experimental.local_worker.gitstate import TreeState, parse_status


class IgnoreWorld(World):
    """git status as git computes it with worktree ignore files: a new .gitignore holding "*"
    ignores every new file in its directory, itself included."""

    def status(self) -> bytes:
        recs = [f"# branch.oid {self.head}", "# branch.head main"]
        star = {
            p.rpartition("/")[0]
            for p, text in self.files.items()
            if is_ignore_file(p) and p not in self.base and text.strip() == "*"
        }
        for p in self.changed():
            if p in self.base:
                recs.append(f"1 .M N... 100644 100644 100644 {'b' * 40} {'b' * 40} {p}")
            else:
                recs.append(f"! {p}" if p.rpartition("/")[0] in star else f"? {p}")
        return ("\0".join(recs) + "\0").encode()


def _entries(turn: TurnLog) -> tuple[str, ...]:
    assert turn.tree is not None
    return turn.tree.entries


def _source_cut(tmp_path, writes):
    replies = [reply("write_file", path=p, content=c) for p, c in writes]
    loop, _, _, _ = make_loop(tmp_path, replies, world=IgnoreWorld())
    out = loop.run(initial_messages(TASK))
    assert all(t.tree is not None and t.tree.observed for t in out.turns[:-1])
    return first_source_edit(out.turns, out.base), out


@pytest.mark.parametrize("name", [".gitignore", ".GitIgnore"])
def test_planting_the_ignore_file_first_is_the_source_cut(tmp_path, name):
    cut, out = _source_cut(tmp_path, [(f"src/{name}", "*\n"), ("src/new.py", "x = 1\n")])
    assert any(e == f"! src/{name}" for e in _entries(out.turns[0]))  # it ignores itself
    assert "! src/new.py" in _entries(out.turns[1])  # and hides the new source file
    assert cut["cut_status"] == "CUT" and cut["turn_index"] == 0 and cut["cause"] == "write_file"


def test_a_source_file_written_before_the_ignore_file_is_still_the_source_cut(tmp_path):
    cut, out = _source_cut(tmp_path, [("src/new.py", "x = 1\n"), ("src/.gitignore", "*\n")])
    assert "? src/new.py" in _entries(out.turns[0])
    assert "! src/new.py" in _entries(out.turns[1])  # hidden only afterwards
    assert cut["cut_status"] == "CUT" and cut["turn_index"] == 0 and cut["cause"] == "write_file"


def test_an_ignored_artifact_is_still_no_source_edit():
    base = TreeState(observed=True, head=BASE_SHA)
    tree = TreeState(observed=True, head=BASE_SHA, entries=("! build/out.py", "! x.log"))
    turn = TurnLog(index=0, t_start=0.0, action_type="run_test", tool_ran=True, tree=tree)
    assert first_source_edit([turn], base)["cut_status"] == "NO_CUT"


@pytest.mark.sandbox
def test_real_git_lists_a_self_ignoring_gitignore_as_ignored(tmp_path):
    """Pins what the rule relies on: git's own status for the planted file."""
    runs = make_runs(tmp_path)
    co, root = runs / TAG / "checkout", str(runs / TAG)
    (co / ".git").rmdir()
    (co / "sub" / "keep.txt").write_text("k\n")
    (co / "src").mkdir()
    (co / "src" / "app.py").write_text("x = 1\n")
    git = ["git", "-C", str(co), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    base = parse_status(run(runs, "status", root).stdout)
    assert run(runs, "write", root, "src/.gitignore", stdin=b"*\n").returncode == 0
    assert run(runs, "write", root, "src/new.py", stdin=b"y = 2\n").returncode == 0
    tree = parse_status(run(runs, "status", root).stdout)
    assert tree.entries == ("! src/.gitignore", "! src/new.py")
    turn = TurnLog(index=0, t_start=0.0, action_type="run_test", tool_ran=True, tree=tree)
    assert first_source_edit([turn], base)["cut_status"] == "CUT"
