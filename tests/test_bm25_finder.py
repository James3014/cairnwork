"""experimental/bm25_hints on-demand finder: named-first ranking, commit source, worker seam."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from experimental.bm25_hints.commit_source import CommitSourceError, commit_texts
from experimental.bm25_hints.find import MAX_K, find_files, named_files
from experimental.bm25_hints.tool import (
    ENV_FLAG,
    MAX_REQUESTS,
    REQUEST_PREFIX,
    TOOL_DESCRIPTION,
    FinderConfig,
    offer,
    requests_in,
    respond,
)
from tests.test_bm25_hints import FIXTURE, git, make_repo

ITEMS = [
    ("app/parser.py", "def parse_timezone(value):\n    return tz(value)\n"),
    ("app/render.py", "def render_page(template):\n    return template\n"),
    ("docs/render.md", "rendering notes\n"),
    ("tests/test_parser.py", "def test_parse_timezone():\n    pass\n"),
]


def test_named_files_exact_suffix_and_basename():
    files = [p for p, _ in ITEMS]
    assert named_files("see app/parser.py", files) == ["app/parser.py"]
    assert named_files("in ./parser.py and render.md", files) == ["app/parser.py", "docs/render.md"]
    assert named_files("nothing named here", files) == []


def test_named_files_come_first_then_bm25():
    hits = find_files(ITEMS, "render.md is stale; parse_timezone too", k=3)
    assert [(h.path, h.reason) for h in hits][:2] == [
        ("docs/render.md", "named"),
        ("app/parser.py", "bm25"),
    ]
    assert hits[0].score == 0.0


def test_named_test_file_is_listed_but_bm25_skips_tests():
    hits = find_files(ITEMS, "tests/test_parser.py parse_timezone", k=5)
    assert hits[0].path == "tests/test_parser.py" and hits[0].reason == "named"
    assert [h.path for h in hits].count("tests/test_parser.py") == 1
    assert "tests/test_parser.py" not in [h.path for h in find_files(ITEMS, "parse_timezone")]


def test_k_is_capped_and_non_negative():
    many = [(f"m/f{i}.py", "token\n") for i in range(MAX_K + 10)]
    assert len(find_files(many, "token", k=10_000)) == MAX_K
    assert find_files(many, "token", k=-1) == []


def test_commit_texts_reads_the_commit_not_the_worktree(tmp_path):
    repo = make_repo(tmp_path / "r", {**FIXTURE, "bin.dat": b"\0\1\2", "big/x.txt": "ok\n"})
    base = git(repo, "rev-parse", "HEAD")
    (repo / "app" / "utils.py").write_text("changed in the worktree\n")
    (repo / "untracked.py").write_text("x = 1\n")
    got = dict(commit_texts(repo, base))
    assert "bin.dat" not in got and "untracked.py" not in got
    assert got["app/utils.py"] == "def helper():\n    return 1\n"
    assert set(got) == set(FIXTURE) | {"big/x.txt"}


def test_commit_texts_skips_symlinks(tmp_path):
    repo = make_repo(tmp_path / "r", {"a.py": "x\n"})
    (repo / "link.py").symlink_to("a.py")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "link")
    assert dict(commit_texts(repo, "HEAD")) == {"a.py": "x\n"}


def test_commit_texts_rejects_unknown_commit_and_option_like_names(tmp_path):
    repo = make_repo(tmp_path / "r", {"a.py": "x\n"})
    with pytest.raises(CommitSourceError):
        list(commit_texts(repo, "0" * 40))
    with pytest.raises(CommitSourceError):
        list(commit_texts(repo, "--output=/tmp/x"))


def test_commit_texts_ignores_a_planted_fsmonitor(tmp_path):
    repo = make_repo(tmp_path / "r", {"a.py": "x\n"})
    marker = tmp_path / "ran"
    git(repo, "config", "core.fsmonitor", f"touch {marker}; false")
    assert dict(commit_texts(repo, "HEAD")) == {"a.py": "x\n"}
    assert not marker.exists()


def test_finder_is_off_by_default():
    assert FinderConfig().enabled is False
    assert FinderConfig.from_env({}).enabled is False
    assert FinderConfig.from_env({ENV_FLAG: "true"}).enabled is False
    assert FinderConfig.from_env({ENV_FLAG: "1"}).enabled is True
    assert offer("do X", FinderConfig()) == "do X"
    assert offer("do X", FinderConfig(enabled=True)).endswith(TOOL_DESCRIPTION)


def test_requests_in_parses_dedups_and_caps():
    reply = "\n".join(
        [
            "I need more context.",
            f"{REQUEST_PREFIX} parse_timezone",
            f"  - `{REQUEST_PREFIX} tz parsing helpers`",
            f"{REQUEST_PREFIX} parse_timezone",
            f"{REQUEST_PREFIX}   ",
            f"{REQUEST_PREFIX} third",
            f"{REQUEST_PREFIX} fourth",
            f"mid-line {REQUEST_PREFIX} ignored",
        ]
    )
    assert requests_in(reply) == ["parse_timezone", "tz parsing helpers", "third"]
    assert len(requests_in(reply)) == MAX_REQUESTS
    assert requests_in(f"{REQUEST_PREFIX} " + "a" * 2000)[0] == "a" * 500


def test_respond_off_or_without_request_returns_none(tmp_path):
    repo = make_repo(tmp_path / "r", FIXTURE)
    reply = f"{REQUEST_PREFIX} parse_timezone"
    assert respond(repo, "HEAD", reply, FinderConfig()) is None
    assert respond(repo, "HEAD", "no request", FinderConfig(enabled=True)) is None


def test_respond_answers_from_base_commit(tmp_path):
    repo = make_repo(tmp_path / "r", FIXTURE)
    base = git(repo, "rev-parse", "HEAD")
    reply = f"{REQUEST_PREFIX} parse_timezone\n{REQUEST_PREFIX} zzz_nothing"
    text = respond(repo, base, reply, FinderConfig(enabled=True, k=2))
    assert text is not None
    assert "  1. app/parser.py" in text
    assert "(no matching file)" in text
    assert "tests/test_parser.py" not in text


def test_respond_cleans_file_names_and_query(tmp_path):
    repo = make_repo(tmp_path / "r", {"a/<evil>`\u202e.py": "needle\n"})
    text = respond(repo, "HEAD", f"{REQUEST_PREFIX} needle <x>", FinderConfig(enabled=True))
    assert text is not None
    body = text.split("\n", 1)[1]
    assert not any(c in body for c in "<>`\u202e")


def test_core_does_not_import_the_finder():
    src = Path(__file__).resolve().parents[1] / "src"
    found = subprocess.run(
        ["grep", "-rl", "bm25_hints", str(src)], capture_output=True, text=True
    ).stdout
    assert found == ""
