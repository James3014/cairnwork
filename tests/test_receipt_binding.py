"""PRESERVED #15: the stored Core request must equal a change set recomputed from git (#61)."""
# ruff: noqa: F811

import pytest
from conftest import commit_change, tamper_stored_request
from test_notary import PASS, head_of, spec

from cairnwork.changeset import read_change_set
from cairnwork.notary import Notary


@pytest.fixture
def notary(tmp_path):
    return Notary(tmp_path / "notary.sqlite")


def _bound(notary, repo, base, head, attempt="a1"):
    return notary.bound_to("task-1", attempt, read_change_set(repo, base, head))


def test_the_stored_request_is_bound_to_the_change_read_from_git(repo, notary):
    base = head_of(repo)
    head = commit_change(repo, {"src/a.py": "x = 2\n"}, delete=("src/b.py",))
    s = spec(allowed=("src/a.py", "src/b.py"), deletions=True)
    notary.notarize("task-1", "a1", s, repo, base, head, PASS)
    assert _bound(notary, repo, base, head)
    assert not _bound(notary, repo, base, head, attempt="a2")  # nothing notarized


def test_another_candidate_is_not_the_notarized_one(repo, notary):
    base = head_of(repo)
    head = commit_change(repo, {"src/a.py": "x = 2\n"})
    other = commit_change(repo, {"src/a.py": "x = 3\n"})
    notary.notarize("task-1", "a1", spec(), repo, base, head, PASS)
    assert not _bound(notary, repo, base, other)


def _set_change_set(key, value):
    return lambda request: request["change_set"].__setitem__(key, value)


@pytest.mark.parametrize(
    "mutate",
    [
        _set_change_set("diff_hash", "sha256:" + "0" * 64),
        _set_change_set("paths", ["src/a.py", "src/b.py"]),
        _set_change_set("deleted_paths", ["src/a.py"]),
        _set_change_set("source_revision", "git-commit:" + "1" * 40),
        _set_change_set("target_revision", "git-commit:" + "2" * 40),
        lambda r: r["change_manifest"].__setitem__("target_tree", "git-tree:" + "3" * 40),
        lambda r: r["change_manifest"]["entries"][0].__setitem__("after_oid", "4" * 40),
        lambda r: r.__setitem__("change_set", "not a dict"),
    ],
    ids=["diff_hash", "paths", "deleted_paths", "source", "target", "tree", "entry", "shape"],
)
def test_each_stored_field_is_compared_with_the_recomputed_one(repo, notary, tmp_path, mutate):
    base = head_of(repo)
    head = commit_change(repo, {"src/a.py": "x = 2\n"})
    notary.notarize("task-1", "a1", spec(), repo, base, head, PASS)
    assert _bound(notary, repo, base, head)
    tamper_stored_request(tmp_path / "notary.sqlite", mutate)
    assert not _bound(notary, repo, base, head)
