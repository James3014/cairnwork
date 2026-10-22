"""experimental/bm25_hints: tokenising, tracked-text-only indexing, ranking, eval script."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from experimental.bm25_hints import rank_files
from experimental.bm25_hints.__main__ import main as cli_main
from experimental.bm25_hints.bm25 import tokens
from experimental.bm25_hints.eval_hit_at_k import evaluate, usable_tasks
from experimental.bm25_hints.retrieval import MAX_FILE_BYTES, is_test_path


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def make_repo(root: Path, files: Mapping[str, str | bytes]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q")
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body if isinstance(body, bytes) else body.encode())
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root


FIXTURE = {
    "app/parser.py": "def parse_timezone(value):\n    return tzinfo_from_string(value)\n",
    "app/render.py": "def render_page(template):\n    return template.upper()\n",
    "app/utils.py": "def helper():\n    return 1\n",
    "tests/test_parser.py": "def test_parse_timezone():\n    parse_timezone('x')\n",
    "README.md": "A small app.\n",
}


def test_tokens_split_snake_and_camel():
    t = tokens("parse_timezone HTTPServerError getFooBar")
    assert {"parse_timezone", "parse", "timezone"} <= set(t)
    assert {"httpservererror", "http", "server", "error"} <= set(t)
    assert {"getfoobar", "get", "foo", "bar"} <= set(t)


def test_tokens_plain_word_is_not_duplicated_and_short_parts_dropped():
    assert tokens("Parser") == ["parser"]
    assert tokens("a_b") == ["a_b"]


def test_right_file_ranks_first(tmp_path):
    repo = make_repo(tmp_path / "r", FIXTURE)
    got = rank_files(repo, "arrow.get() mishandles the timezone string in parse")
    assert got[0] == "app/parser.py"
    assert "tests/test_parser.py" not in got


def test_path_tokens_count(tmp_path):
    repo = make_repo(
        tmp_path / "r",
        {"pkg/scheduler.py": "x = 1\n", "pkg/other.py": "scheduler is mentioned once\n" * 1},
    )
    assert rank_files(repo, "scheduler")[0] == "pkg/scheduler.py"


def test_include_tests_flag(tmp_path):
    repo = make_repo(tmp_path / "r", FIXTURE)
    assert "tests/test_parser.py" in rank_files(repo, "parse_timezone", include_tests=True)
    assert "tests/test_parser.py" not in rank_files(repo, "parse_timezone")


def test_is_test_path():
    assert is_test_path("tests/a.py") and is_test_path("pkg/test_x.py")
    assert is_test_path("pkg/x_test.go") and is_test_path("conftest.py")
    assert not is_test_path("pkg/contest.py") and not is_test_path("src/latest.py")


def test_deterministic_and_stable_tie_break(tmp_path):
    repo = make_repo(
        tmp_path / "r", {n: "shared token here\n" for n in ("b.txt", "a.txt", "c.txt")}
    )
    first = rank_files(repo, "shared token")
    assert first == ["a.txt", "b.txt", "c.txt"]
    assert all(rank_files(repo, "shared token") == first for _ in range(3))


def test_k_limits_and_no_match_returns_empty(tmp_path):
    repo = make_repo(tmp_path / "r", FIXTURE)
    assert len(rank_files(repo, "parse render helper", k=2)) == 2
    assert rank_files(repo, "parse render helper", k=0) == []
    assert rank_files(repo, "zzzqqq nothing") == []
    assert rank_files(repo, "") == []


def test_skips_binary_and_large_files(tmp_path):
    repo = make_repo(
        tmp_path / "r",
        {
            "bin.dat": b"needle\0\x01\x02 needle needle",
            "big.txt": ("needle " * 200_000).encode()[: MAX_FILE_BYTES + 1],
            "small.txt": "needle once\n",
        },
    )
    assert rank_files(repo, "needle") == ["small.txt"]


def test_ignores_untracked_and_ignored_files(tmp_path):
    repo = make_repo(tmp_path / "r", {"tracked.py": "needle\n", ".gitignore": "ignored.py\n"})
    (repo / "untracked.py").write_text("needle needle needle\n")
    (repo / "ignored.py").write_text("needle needle needle\n")
    assert rank_files(repo, "needle") == ["tracked.py"]


def test_symlink_and_deleted_tracked_file_are_skipped(tmp_path):
    repo = make_repo(tmp_path / "r", {"a.py": "needle\n", "gone.py": "needle needle\n"})
    (repo / "link.py").symlink_to(repo / "a.py")
    git(repo, "add", "link.py")
    (repo / "gone.py").unlink()
    assert rank_files(repo, "needle") == ["a.py"]


def test_cli_prints_json(tmp_path, capsys):
    repo = make_repo(tmp_path / "r", FIXTURE)
    issue = tmp_path / "issue.md"
    issue.write_text("parse_timezone is wrong")
    assert cli_main([str(repo), str(issue), "-k", "1"]) == 0
    assert json.loads(capsys.readouterr().out) == ["app/parser.py"]


def test_cli_rejects_missing_repo(tmp_path):
    issue = tmp_path / "i.md"
    issue.write_text("x")
    with pytest.raises(subprocess.CalledProcessError):
        cli_main([str(tmp_path / "nope"), str(issue)])


def _bank(tmp_path: Path) -> Path:
    bank = tmp_path / "bank"
    src = make_repo(bank / "src" / "demo", FIXTURE)
    base = git(src, "rev-parse", "HEAD")
    specs = {
        "good": (True, {}, "parse_timezone fails"),
        "bad-fix": (False, {}, "parse_timezone fails"),
        "dropped": (True, {"drop": True}, "parse_timezone fails"),
        "excluded": (True, {"exclude": "tz db"}, "parse_timezone fails"),
        "no-issue": (True, {}, None),
    }
    for name, (ok, check, issue) in specs.items():
        d = bank / "tasks" / name
        d.mkdir(parents=True)
        meta = {"repo": "demo", "base_sha": base, "files_changed": ["app/parser.py"]}
        (d / "meta.json").write_text(json.dumps(meta))
        (d / "deferred_fixcheck.json").write_text(json.dumps({"ok": ok}))
        (d / "issue_check.json").write_text(json.dumps(check))
        if issue:
            (d / "issue.md").write_text(issue)
    return bank


def test_eval_filters_tasks_and_reports_aggregates(tmp_path):
    bank = _bank(tmp_path)
    assert [d.name for d in usable_tasks(bank)] == ["good"]
    result = evaluate(bank)
    assert result["tasks"] == 1 and result["skipped"] == 0
    assert result["hit@1"] == result["hit@5"] == result["hit@10"] == 1
    assert result["hit@1_rate"] == 1.0
    assert all("good" not in str(v) for v in result.values())


def test_eval_counts_unreachable_base_as_skipped(tmp_path):
    bank = _bank(tmp_path)
    meta = bank / "tasks" / "good" / "meta.json"
    meta.write_text(json.dumps({"repo": "demo", "base_sha": "0" * 40, "files_changed": ["x"]}))
    assert evaluate(bank)["skipped"] == 1
