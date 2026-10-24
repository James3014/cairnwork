"""The git queries' scratch directory <run_root>/q (Claude review of #239, NB1).

Test code runs as the same uid as the git queries. Before the fix the queries wrote their output
under <run_root>/tmp, which test code left running after a pytest call could rewrite between
git writing a result and the wrapper printing it. Now they write only in <run_root>/q, under a
query profile; the tool profile (every line of test code) can neither read nor write q.
"""

import os
import subprocess
import sys
import time

import pytest
from local_worker_fakes import NO_AUTO_MAINTENANCE, TAG, WRAPPER, make_runs
from local_worker_fakes import run_wrapper as run

WRITER = """
import os, time
tmp = os.environ["TMPDIR"]
root = os.path.dirname(tmp)
q = os.path.join(root, "q")
def note(name, text):
    with open(os.path.join(tmp, name + ".part"), "w") as fh:
        fh.write(text)
    os.replace(os.path.join(tmp, name + ".part"), os.path.join(tmp, name))
try:
    with open(os.path.join(q, "planted"), "w") as fh:
        fh.write("x")
    wrote = "WROTE"
except OSError:
    wrote = "DENIED"
try:
    os.listdir(q)
    listed = "LISTED"
except OSError:
    listed = "DENIED"
note("probe", wrote + " " + listed)
fake_status = b"# branch.oid HEAD_SHA\\0# branch.head main\\0"
writes = 0
end = time.time() + 120
while time.time() < end and not os.path.exists(os.path.join(tmp, "stop")):
    for d in (q, tmp):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            if not n.startswith("gitq."):
                continue
            g = os.path.join(d, n)
            fake = b"" if os.path.exists(os.path.join(g, "index")) else fake_status
            try:
                with open(os.path.join(g, "out"), "wb") as fh:
                    fh.write(fake)
                writes += 1
            except OSError:
                pass
note("writes", str(writes))
"""


@pytest.fixture
def runs(tmp_path):
    return make_runs(tmp_path)


def _git_checkout(runs):
    co = runs / TAG / "checkout"
    (co / ".git").rmdir()
    (co / "sub" / "keep.txt").write_text("k\n")
    git = ["git", "-C", str(co), "-c", "user.email=t@t", "-c", "user.name=t", *NO_AUTO_MAINTENANCE]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    head = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
    return co, str(runs / TAG), head.stdout.strip()


def _wait_for(path, seconds=60.0):
    end = time.monotonic() + seconds
    while not path.exists():
        assert time.monotonic() < end, f"{path.name} never appeared"
        time.sleep(0.05)
    return path.read_text()


def test_wrapper_refuses_a_run_root_without_q(runs):
    (runs / TAG / "q").rmdir()
    proc = run(runs, "status", str(runs / TAG))
    assert proc.returncode == 64 and b"q directory" in proc.stderr


def test_query_and_tool_profiles_are_separate():
    """The tool profile denies q outright; the queries run under their own profile in q."""
    text = WRAPPER.read_text()
    tool = text[text.index("profile() {  # the tool profile") : text.index("query_profile() {")]
    assert '(deny file-read* file-write* (subpath "%s/q"))' in tool
    query = text[text.index("query_profile() {") : text.index("# sandboxed <profile text>")]
    assert '(allow file-write* (subpath "%s/q") (literal "/dev/null"))' in query
    assert "/tmp" not in query and "venv" not in query
    assert 'sandboxed "$(query_profile)" "$RUN_ROOT/q" /bin/sh -c "$GIT_QUERY"' in text
    assert text.count('sandboxed "$(query_profile)"') == 1


@pytest.mark.sandbox
def test_sandboxed_writer_loop_cannot_falsify_status_or_numstat(runs):
    """Test code left running after pytest (here never swept until the end) rewrites every
    gitq.*/out it can find with a clean status or an empty numstat, as fast as it can. Every
    query still reports the real change, and the writer could not even list or write q."""
    from experimental.local_worker.gitstate import parse_numstat, parse_status

    co, root, head = _git_checkout(runs)
    tmp = runs / TAG / "tmp"
    (tmp / "writer.py").write_text(WRITER.replace("HEAD_SHA", head))
    base = os.path.realpath(getattr(sys, "_base_executable", sys.executable))
    py = runs / TAG / "venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text(f"#!/bin/sh\n{base} {tmp / 'writer.py'} </dev/null >/dev/null 2>&1 &\nexit 0\n")
    py.chmod(0o755)
    assert run(runs, "write", root, "new.py", stdin=b"one\ntwo\n").returncode == 0
    try:
        assert run(runs, "pytest", root).returncode == 0  # returns; the writer keeps running
        assert _wait_for(tmp / "probe") == "DENIED DENIED"  # cannot write, cannot even list q
        for _ in range(30):
            status = run(runs, "status", root)
            tree = parse_status(status.stdout)
            assert status.returncode == 0 and tree.observed and tree.head == head
            assert "? new.py" in tree.entries, status.stdout
            numstat = run(runs, "numstat", root)
            assert numstat.returncode == 0 and parse_numstat(numstat.stdout) == (2, 0, 0)
        (tmp / "stop").write_text("")
        assert _wait_for(tmp / "writes") == "0"  # no gitq.* directory was ever in its reach
    finally:
        (tmp / "stop").write_text("")
        assert run(runs, "sweep", root).stdout == b"LOCAL_WORKER_SWEEP clean\n"
    assert not (runs / TAG / "q" / "planted").exists()
    assert list((runs / TAG / "q").iterdir()) == []  # every query removed its own directory
