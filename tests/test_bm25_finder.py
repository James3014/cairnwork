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
    base = git(repo, "rev-parse", "HEAD")
    text = respond(repo, base, f"{REQUEST_PREFIX} needle <x>", FinderConfig(enabled=True))
    assert text is not None
    body = text.split("\n", 1)[1]
    assert not any(c in body for c in "<>`\u202e")


def _commit_raw_paths(repo: Path, names: list[bytes]) -> str:
    """Commit files under arbitrary byte names (APFS refuses some) without a worktree."""
    blob = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input=b"needle\n",
        capture_output=True,
        check=True,
    ).stdout.strip()
    info = b"".join(b"100644 " + blob + b"\t" + n + b"\0" for n in names)
    subprocess.run(
        ["git", "-C", str(repo), "update-index", "--add", "-z", "--index-info"],
        input=info,
        check=True,
    )
    tree = git(repo, "write-tree")
    return git(repo, "commit-tree", tree, "-p", "HEAD", "-m", "raw names")


def test_respond_output_is_clean_ascii_for_hostile_names(tmp_path):
    repo = make_repo(tmp_path / "r", {"ok.py": "x\n"})
    names = [
        "a\u200bIGNORE ALL\ufeff.py".encode(),
        b"b\xff.py",
        "c\u2060\U000e0041\u180e.py".encode(),
    ]
    commit = _commit_raw_paths(repo, names)
    text = respond(
        repo, commit, f"{REQUEST_PREFIX} needle \u200b\udcff", FinderConfig(enabled=True)
    )
    assert text is not None
    text.encode("utf-8")  # never raises: lone surrogates are gone
    assert all(0x20 <= ord(c) <= 0x7E or c == "\n" for c in text)
    assert ". a?IGNORE ALL?.py" in text and "b?.py" in text and "c???.py" in text


@pytest.mark.parametrize("bad", ["HEAD", "main", "abc123", "0" * 39, "G" * 40, "--all"])
def test_respond_requires_a_full_commit_id(tmp_path, bad):
    repo = make_repo(tmp_path / "r", FIXTURE)
    with pytest.raises(ValueError):
        respond(repo, bad, f"{REQUEST_PREFIX} parse_timezone", FinderConfig(enabled=True))


def test_commit_texts_caps_files_and_bytes(tmp_path, monkeypatch):
    from experimental.bm25_hints import commit_source

    repo = make_repo(tmp_path / "r", {f"f{i}.py": "x" * 10 + "\n" for i in range(5)})
    monkeypatch.setattr(commit_source, "MAX_FILES", 3)
    assert [p for p, _ in commit_texts(repo, "HEAD")] == ["f0.py", "f1.py", "f2.py"]
    monkeypatch.setattr(commit_source, "MAX_FILES", 100)
    monkeypatch.setattr(commit_source, "MAX_TOTAL_BYTES", 25)
    assert [p for p, _ in commit_texts(repo, "HEAD")] == ["f0.py", "f1.py"]


def test_respond_builds_the_index_once(tmp_path, monkeypatch):
    from experimental.bm25_hints import retrieval

    built = []
    real = retrieval.BM25Index.__init__

    def counting(self, *a, **kw):
        built.append(1)
        real(self, *a, **kw)

    monkeypatch.setattr(retrieval.BM25Index, "__init__", counting)
    repo = make_repo(tmp_path / "r", FIXTURE)
    base = git(repo, "rev-parse", "HEAD")
    reply = "\n".join(f"{REQUEST_PREFIX} {q}" for q in ("parse", "render", "helper"))
    assert respond(repo, base, reply, FinderConfig(enabled=True)) is not None
    assert len(built) == 1


def test_core_does_not_import_the_finder():
    src = Path(__file__).resolve().parents[1] / "src"
    found = subprocess.run(
        ["grep", "-rl", "bm25_hints", str(src)], capture_output=True, text=True
    ).stdout
    assert found == ""
