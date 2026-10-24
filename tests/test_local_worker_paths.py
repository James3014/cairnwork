"""Model-chosen path names never reach the corpus line (Claude review of #239, NB4).

A path is readable in the line only if it existed at base (the worker's ``tracked`` query lists
every file and directory of HEAD); a new path, even after a successful write_file, becomes
{sha256, bytes, ext}. The rule covers tool arguments, target_file, changed_paths and
first_source_edit.path; with no base listing every path is a digest, and the validator holds a
line to that.
"""

import copy
import json
import subprocess

import pytest
from local_worker_fakes import (
    META,
    NO_AUTO_MAINTENANCE,
    TAG,
    TASK,
    World,
    make_loop,
    make_runs,
    reply,
)
from local_worker_fakes import run_wrapper as run

from experimental.local_worker import episode
from experimental.local_worker.agent import initial_messages
from experimental.local_worker.episode_schema import SchemaError, validate_record
from experimental.local_worker.gitstate import parse_tracked
from experimental.local_worker.redact import is_path_digest, path_digest, path_ext

SECRET = "SECRET-NAME-4411"


def _record(tmp_path, replies, world=None):
    loop, _, _, _ = make_loop(tmp_path, replies, world)
    rec = episode.build_record(META, loop.run(initial_messages(TASK)))
    validate_record(rec)
    return rec


def test_only_paths_that_existed_at_base_stay_readable(tmp_path):
    replies = [
        reply("list_dir", path="src"),
        reply("read_file", path="src/app.py"),
        reply("write_file", path=f"src/{SECRET}/mod.py", content="y = 1\n"),
        reply("list_dir", path=f"src/{SECRET}"),
        reply("edit_file", path="src/app.py", target="x = 1", replacement="x = 2"),
    ]
    rec = _record(tmp_path, replies)
    assert rec["base_paths_observed"] is True
    paths = [t["tool_args"].get("path") for t in rec["turns"][:5]]
    assert paths[0] == "src" and paths[1] == "src/app.py" and paths[4] == "src/app.py"
    assert paths[2] == path_digest(f"src/{SECRET}/mod.py") and paths[2]["ext"] == ".py"
    assert paths[3] == path_digest(f"src/{SECRET}") and paths[3]["ext"] is None
    cut, source = rec["first_edit_cut"], rec["first_source_edit"]
    assert cut["target_file"] == path_digest(f"src/{SECRET}/mod.py")
    assert cut["changed_paths"] == [path_digest(f"src/{SECRET}/mod.py")]
    assert source["path"] == path_digest(f"src/{SECRET}/mod.py")
    assert SECRET not in json.dumps(rec)


def test_a_changed_base_file_stays_readable_in_the_cut(tmp_path):
    rec = _record(tmp_path, [reply("write_file", path="src/app.py", content="x = 3\n")])
    cut = rec["first_edit_cut"]
    assert cut["target_file"] == "src/app.py" and cut["changed_paths"] == ["src/app.py"]
    assert rec["first_source_edit"]["path"] == "src/app.py"


class NoListing(World):
    def handle(self, action, args, stdin):
        if action == "tracked":
            return 3, b"fatal: something went wrong\n"
        return super().handle(action, args, stdin)


def test_without_the_base_listing_every_path_is_a_digest(tmp_path):
    replies = [
        reply("read_file", path="src/app.py"),
        reply("write_file", path="src/app.py", content="x = 3\n"),
    ]
    rec = _record(tmp_path, replies, NoListing())
    assert rec["base_paths_observed"] is False
    assert [t["tool_args"]["path"] for t in rec["turns"][:2]] == [path_digest("src/app.py")] * 2
    assert rec["first_edit_cut"]["target_file"] == path_digest("src/app.py")
    assert rec["first_source_edit"]["path"] == path_digest("src/app.py")
    assert "src/app.py" not in json.dumps(rec)
    for where, value in (
        (("turns", 0, "tool_args", "path"), "src/app.py"),
        (("first_edit_cut", "target_file"), "src/app.py"),
        (("first_edit_cut", "changed_paths"), ["src/app.py"]),
        (("first_source_edit", "path"), "src/app.py"),
    ):
        bad = copy.deepcopy(rec)
        node = bad
        for key in where[:-1]:
            node = node[key]
        node[where[-1]] = value
        with pytest.raises(SchemaError):
            validate_record(bad)
    rec["base_paths_observed"] = True  # the same plain paths are fine once a listing was seen
    rec["first_edit_cut"]["target_file"] = "src/app.py"
    validate_record(rec)


@pytest.mark.parametrize(
    "bad",
    [
        {"sha256": "a" * 64, "bytes": 3},  # a text digest is not a path digest
        {"sha256": "a" * 64, "bytes": 3, "ext": ".secret"},  # not an allowlisted extension
        {"sha256": "a" * 64, "bytes": 3, "ext": None, "name": "x"},
    ],
)
def test_the_validator_takes_only_well_formed_path_digests(tmp_path, bad):
    rec = _record(tmp_path, [reply("write_file", path="new.py", content="z\n")])
    rec["first_edit_cut"]["changed_paths"] = [bad]
    with pytest.raises(SchemaError, match="changed_paths"):
        validate_record(rec)


def test_extensions_come_only_from_the_allowlist():
    assert path_ext("a/b.PY") == ".py" and path_ext("x/.gitignore") == ".gitignore"
    assert path_ext(f"a/b.{SECRET}") is None and path_ext("noext") is None
    assert is_path_digest(path_digest(f"x.{SECRET}"))
    assert parse_tracked(b"a\0b/c\0") == frozenset({".", "a", "b/c"})
    assert parse_tracked(b"") == frozenset({"."})
    assert parse_tracked(b"a\0b") is None and parse_tracked(b"a\0\0") is None


@pytest.mark.sandbox
def test_tracked_lists_the_base_files_and_directories(tmp_path):
    runs = make_runs(tmp_path)
    co, root = runs / TAG / "checkout", str(runs / TAG)
    (co / ".git").rmdir()
    (co / "sub" / "keep.txt").write_text("k\n")
    git = ["git", "-C", str(co), "-c", "user.email=t@t", "-c", "user.name=t", *NO_AUTO_MAINTENANCE]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    assert run(runs, "write", root, "new.py", stdin=b"n\n").returncode == 0  # not in HEAD
    proc = run(runs, "tracked", root)
    assert proc.returncode == 0
    assert parse_tracked(proc.stdout) == frozenset({".", "a.txt", "sub", "sub/keep.txt"})
    refused = run(runs, "tracked", root, "HEAD~1")
    assert refused.returncode == 64 and b"refusing" in refused.stderr
