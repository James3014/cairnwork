import pytest
from conftest import commit_change, git

from cairnwork.changeset import ChangeSetError, read_change_set


def test_modify_add_and_delete_are_read_from_git(repo):
    base = git(repo, "rev-parse", "HEAD")
    head = commit_change(
        repo, {"src/a.py": "x = 2\n", "src/new.py": "z = 1\n"}, delete=["src/b.py"]
    )
    change = read_change_set(repo, base, head)
    kinds = {entry.path: entry.change_type for entry in change.entries}
    assert kinds == {"src/a.py": "MODIFY", "src/b.py": "DELETE", "src/new.py": "ADD"}
    assert change.deleted_paths == ["src/b.py"]
    added = next(e for e in change.entries if e.path == "src/new.py")
    assert added.before_oid is None and added.before_mode is None and added.after_mode == "100644"


def test_rename_is_split_into_delete_and_add(repo):
    base = git(repo, "rev-parse", "HEAD")
    head = commit_change(repo, rename=("src/a.py", "src/c.py"))
    kinds = {e.path: e.change_type for e in read_change_set(repo, base, head).entries}
    assert kinds == {"src/a.py": "DELETE", "src/c.py": "ADD"}


@pytest.mark.parametrize("base,head", [("HEAD", "f" * 40), ("a" * 40, "a" * 40), ("abc", "d" * 40)])
def test_invalid_commit_ids_are_rejected(repo, base, head):
    with pytest.raises(ChangeSetError):
        read_change_set(repo, base, head)


def test_unknown_commit_is_rejected(repo):
    base = git(repo, "rev-parse", "HEAD")
    with pytest.raises(ChangeSetError):
        read_change_set(repo, base, "f" * 40)


def test_empty_change_is_rejected(repo):
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "commit", "-q", "--allow-empty", "-m", "empty")
    with pytest.raises(ChangeSetError):
        read_change_set(repo, base, git(repo, "rev-parse", "HEAD"))
