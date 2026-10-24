"""numstat counts every changed line, binary or not (Claude review of #239, NB2).

``git diff --numstat`` shows a file it takes for binary (one NUL byte) as ``-`` lines and
``--text`` does not change that, so the old sum let one NUL byte hide a file's whole patch. The
wrapper now sums the hunk headers of the ``--text -U0`` patch and reports the binary count too.
"""

import subprocess

import pytest
from local_worker_fakes import NO_AUTO_MAINTENANCE, TAG, make_runs
from local_worker_fakes import run_wrapper as run

from experimental.local_worker.gitstate import parse_numstat


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (b"PATCH 0 0 0\n", (0, 0, 0)),
        (b"PATCH 12 3 1\n", (12, 3, 1)),
        (b"PATCH 1 2\n", None),
        (b"PATCH 1 2 3", None),  # no newline: cut short
        (b"PATCH 1 2 3\nPATCH 0 0 0\n", None),
        (b"1\t2\tx.py\0", None),  # the old numstat records are not a count
        (b"", None),
        (b"PATCH -1 2 3\n", None),
    ],
)
def test_numstat_line_parses_exactly(raw, want):
    assert parse_numstat(raw) == want


@pytest.mark.sandbox
def test_a_nul_byte_no_longer_hides_a_files_lines(tmp_path):
    runs = make_runs(tmp_path)
    co, root = runs / TAG / "checkout", str(runs / TAG)
    (co / ".git").rmdir()
    (co / "sub" / "keep.txt").write_text("k\n")
    lines = "".join(f"line {i}\n" for i in range(10))
    (co / "big.py").write_text(lines)
    (co / "sql.txt").write_text("-- a comment\n++ not a header\nkeep\n")
    git = ["git", "-C", str(co), "-c", "user.email=t@t", "-c", "user.name=t", *NO_AUTO_MAINTENANCE]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    assert parse_numstat(run(runs, "numstat", root).stdout) == (0, 0, 0)
    # One NUL byte in line 5 of a tracked text file: numstat says "-  -", the patch 1 and 1.
    nul = lines.replace("line 5\n", "line\x005\n").encode()
    assert run(runs, "write", root, "big.py", stdin=nul).returncode == 0
    assert parse_numstat(run(runs, "numstat", root).stdout) == (1, 1, 1)
    # A new binary file counts its lines; a removed "-- " line and an added "@@" line count once.
    assert run(runs, "write", root, "new.bin", stdin=b"a\0b\nc\nd").returncode == 0
    assert run(runs, "write", root, "sql.txt", stdin=b"keep\n@@ -1,99 +1,99 @@\n").returncode == 0
    stat = run(runs, "numstat", root)
    assert stat.returncode == 0 and parse_numstat(stat.stdout) == (1 + 3 + 1, 1 + 2, 2), stat.stdout


def test_the_line_records_the_binary_count_next_to_the_line_counts(tmp_path):
    from local_worker_fakes import META, TASK, make_loop, reply

    from experimental.local_worker import episode
    from experimental.local_worker.agent import initial_messages
    from experimental.local_worker.episode_schema import validate_record

    write = reply("write_file", path="src/app.py", content="x = 2\x00\ny\n")
    loop, _, _, _ = make_loop(tmp_path, [write])
    rec = episode.build_record(META, loop.run(initial_messages(TASK)))
    validate_record(rec)
    keys = ("patch_lines_added", "patch_lines_removed", "binary_files_changed")
    assert [rec["verification"][k] for k in keys] == [2, 1, 1]
