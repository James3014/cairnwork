"""overlay-v1 (#229): how deferred tests are laid over a candidate, shared by the verifier and the
receiver. Regular files only, each test path replaces or adds exactly that path, plumbing only."""

import subprocess
from pathlib import Path

import pytest
from conftest import commit_change, git

from cairnwork.deferred_tests import TestCommitRejected, lay_tests_over, overlay_tree


def _blob(repo, text):
    return subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input=text, capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip


@pytest.fixture
def three(repo):
    """base (with tests/test_a.py and tests/run.sh), head and a test commit, both on base."""
    base = commit_change(repo, {"tests/test_a.py": "def test_a(): pass\n", "tests/run.sh": "x\n"})
    head = commit_change(repo, {"src/a.py": "x = 2\n", "tests/run.sh": "y\n"})
    git(repo, "checkout", "-q", "--detach", base)
    commit_change(repo, {"tests/test_a.py": "def test_a(): assert 1\n",
                         "tests/deep/new/test_x.py": "def test_x(): pass\n"})  # fmt: skip
    git(repo, "update-index", "--chmod=+x", "tests/run.sh")
    git(repo, "update-index", "--add", "--cacheinfo", f"120000,{_blob(repo, 'x')},tests/link")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{base},tests/sub")
    git(repo, "commit", "-q", "-m", "tests")
    tests = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-f", "main")  # the worktree lacks the link and the gitlink
    return base, head, tests


def _entries(repo, tree):
    out = git(repo, "ls-tree", "-r", tree).splitlines()
    return {line.split("\t")[1]: line.split("\t")[0].split()[::2] for line in out}


def test_regular_files_replace_or_add_exactly_their_paths(repo, three, tmp_path):
    base, head, tests = three
    paths = ("tests/deep/new/test_x.py", "tests/run.sh", "tests/test_a.py")
    tree = overlay_tree(repo, head, tests, paths, tmp_path / "i")
    got, in_head, in_tests = (_entries(repo, t) for t in (tree, head, tests))
    for path in paths:  # nested new directory, exec mode, a path already in head
        assert got[path] == in_tests[path]
    assert got["tests/run.sh"][0] == "100755"
    assert {p: e for p, e in got.items() if p not in paths} == {
        p: e for p, e in in_head.items() if p not in paths
    }


@pytest.mark.parametrize("path", ["tests/link", "tests/sub", "tests/missing.py", "tests/deep"])
def test_symlinks_gitlinks_dirs_and_missing_paths_are_unsupported(repo, three, tmp_path, path):
    _base, head, tests = three
    with pytest.raises(TestCommitRejected) as exc:
        overlay_tree(repo, head, tests, (path,), tmp_path / "i")
    assert exc.value.code == "OVERLAY_UNSUPPORTED_ENTRY"


@pytest.mark.parametrize(
    "path", ["tests/../src/a.py", "tests/.git/x", "tests/.GIT/x", "/tests/x", "tests//x", "./x"]
)
def test_dot_dot_and_dot_git_paths_are_unsupported(repo, three, tmp_path, path):
    _base, head, tests = three
    with pytest.raises(TestCommitRejected) as exc:
        overlay_tree(repo, head, tests, (path,), tmp_path / "i")
    assert exc.value.code == "OVERLAY_UNSUPPORTED_ENTRY"


def test_the_tree_ignores_worktree_attributes_and_config(repo, three, tmp_path):
    _base, head, tests = three
    paths = ("tests/test_a.py",)
    first = overlay_tree(repo, head, tests, paths, tmp_path / "i1")
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", "--no-local", str(repo), str(other))
    git(other, "fetch", "-q", str(repo), head, tests)
    git(other, "config", "core.autocrlf", "true")
    (other / ".gitattributes").write_text("* text eol=crlf\n")
    (other / "tests").mkdir(exist_ok=True)
    (other / "tests/test_a.py").write_text("dirty worktree\n")
    assert overlay_tree(other, head, tests, paths, tmp_path / "i2") == first


def test_lay_tests_over_commits_the_overlay_tree(repo, three, tmp_path):
    _base, head, tests = three
    paths = ("tests/test_a.py",)
    (tmp_path / "runs").mkdir()
    judged = lay_tests_over(repo, head, tests, paths, "a0123456789abcdef", tmp_path / "runs")
    expected = overlay_tree(repo, head, tests, paths, tmp_path / "i")
    assert git(repo, "rev-parse", f"{judged}^{{tree}}") == expected
    assert git(repo, "rev-parse", f"{judged}^") == head
    assert not list(Path(tmp_path / "runs").iterdir())  # the private index is gone
