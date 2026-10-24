"""What git cannot see still changes the observed tree (Claude review of #239, NB10).

Empty directories, FIFOs and sockets that test code creates in the checkout are invisible to git
status; the status query lists them after git's records ("x d" / "x o"), so first_edit_cut sees
them (a resume prefix before the cut must leave the tree at base). They are never a source edit.
"""

import os
import socket
import subprocess

import pytest
from local_worker_fakes import BASE_SHA, TAG, make_runs
from local_worker_fakes import run_wrapper as run

from experimental.local_worker.cuts import CUT_DEFINITION, first_edit_cut, first_source_edit
from experimental.local_worker.episode import TurnLog
from experimental.local_worker.gitstate import TreeState, parse_status


@pytest.mark.sandbox
def test_status_lists_empty_dirs_fifos_and_sockets(tmp_path):
    runs = make_runs(tmp_path)
    co, root = runs / TAG / "checkout", str(runs / TAG)
    (co / ".git").rmdir()
    (co / "sub" / "keep.txt").write_text("k\n")
    git = ["git", "-C", str(co), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    assert parse_status(run(runs, "status", root).stdout).entries == ()
    (co / "e1").mkdir()
    (co / "src" / "pkg" / "e2").mkdir(parents=True)
    (co / "name with space").mkdir()
    os.mkfifo(co / "sub" / "pipe")
    sock, cwd = socket.socket(socket.AF_UNIX), os.getcwd()
    try:
        os.chdir(co)  # a relative bind: the absolute path is longer than sun_path allows
        sock.bind("sock")
    finally:
        os.chdir(cwd)
    proc = run(runs, "status", root)
    tree = parse_status(proc.stdout)
    assert proc.returncode == 0 and tree.observed, proc.stdout
    want = ("x d e1", "x d name with space", "x d src/pkg/e2", "x o sock", "x o sub/pipe")
    assert tree.entries == want
    assert tree.paths() == ["e1", "name with space", "sock", "src/pkg/e2", "sub/pipe"]
    for p in (co / "e1", co / "src" / "pkg" / "e2", co / "src" / "pkg", co / "src"):
        p.rmdir()
    (co / "name with space").rmdir()
    (co / "sub" / "pipe").unlink()
    sock.close()
    (co / "sock").unlink()
    assert parse_status(run(runs, "status", root).stdout).entries == ()


def test_an_empty_directory_is_a_tree_change_but_no_source_edit():
    base = TreeState(observed=True, head=BASE_SHA)
    tree = TreeState(observed=True, head=BASE_SHA, entries=("x d src/empty", "x o src/fifo"))
    turn = TurnLog(index=0, t_start=0.0, action_type="run_test", tool_ran=True, tree=tree)
    edit = first_edit_cut([turn], base)
    assert edit["cut_status"] == "CUT" and edit["cut_cause"] == "run_tests_side_effect"
    assert edit["changed_paths_total"] == 2
    assert first_source_edit([turn], base)["cut_status"] == "NO_CUT"
    assert "empty directories, FIFOs and sockets" in CUT_DEFINITION


def test_unseen_records_must_be_find_paths():
    assert not parse_status(b"x d /abs\0").observed
    assert not parse_status(b"x q ./a\0").observed
    assert parse_status(b"x d .\0").entries == ("x d .",)
