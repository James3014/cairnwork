import pytest
from conftest import git

from cairnwork.workspace import (
    CandidateRejected,
    WorkspaceError,
    check_candidate,
    create_attempt_worktree,
    remove_attempt_worktree,
)

ALLOWED = ("src/a.py",)


@pytest.fixture
def attempt(repo, tmp_path):
    base = git(repo, "rev-parse", "HEAD")
    path = create_attempt_worktree(repo, base, tmp_path / "attempts", "a1")
    return base, path


def commit(path, message="candidate"):
    git(path, "add", "-A")
    git(
        path,
        "-c",
        "user.email=w@example.invalid",
        "-c",
        "user.name=w",
        "commit",
        "-q",
        "-m",
        message,
    )
    return git(path, "rev-parse", "HEAD")


def rejected_code(path, base, **kwargs):
    with pytest.raises(CandidateRejected) as info:
        check_candidate(path, base, ALLOWED, **kwargs)
    return info.value.code


def test_new_worktree_is_a_clean_detached_checkout_of_base(repo, attempt):
    base, path = attempt
    assert git(path, "rev-parse", "HEAD") == base
    assert git(path, "status", "--porcelain") == ""
    assert git(path, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"


def test_worktree_path_is_never_reused(repo, attempt, tmp_path):
    base, _ = attempt
    with pytest.raises(WorkspaceError):
        create_attempt_worktree(repo, base, tmp_path / "attempts", "a1")


@pytest.mark.parametrize("attempt_id", ["", "a/b", "..", "."])
def test_attempt_id_must_be_one_path_component(repo, tmp_path, attempt_id):
    with pytest.raises(WorkspaceError):
        create_attempt_worktree(repo, git(repo, "rev-parse", "HEAD"), tmp_path, attempt_id)


@pytest.mark.parametrize("base", ["HEAD", "abc", "f" * 40])
def test_base_must_be_an_existing_full_commit_id(repo, tmp_path, base):
    with pytest.raises(WorkspaceError):
        create_attempt_worktree(repo, base, tmp_path, "a1")


def test_one_in_scope_commit_is_accepted(attempt):
    base, path = attempt
    (path / "src" / "a.py").write_text("x = 2\n")
    head = commit(path)
    candidate = check_candidate(path, base, ALLOWED)
    assert candidate.candidate_commit == head
    assert candidate.paths == ("src/a.py",)


def test_no_commit_is_rejected(attempt):
    base, path = attempt
    assert rejected_code(path, base) == "NO_COMMIT"


def test_two_commits_are_rejected(attempt):
    base, path = attempt
    (path / "src" / "a.py").write_text("x = 2\n")
    commit(path)
    (path / "src" / "a.py").write_text("x = 3\n")
    commit(path)
    assert rejected_code(path, base) == "NOT_ONE_COMMIT_ON_BASE"


def test_uncommitted_or_untracked_changes_are_rejected(attempt):
    base, path = attempt
    (path / "src" / "a.py").write_text("x = 2\n")
    commit(path)
    (path / "notes.txt").write_text("left behind\n")
    assert rejected_code(path, base) == "DIRTY_TREE"


def test_out_of_scope_change_is_rejected(attempt):
    base, path = attempt
    (path / "src" / "b.py").write_text("y = 2\n")
    commit(path)
    assert rejected_code(path, base) == "OUT_OF_SCOPE"


def test_deletion_needs_permission(attempt):
    base, path = attempt
    (path / "src" / "a.py").unlink()
    commit(path)
    assert rejected_code(path, base) == "DELETION_FORBIDDEN"
    assert check_candidate(path, base, ALLOWED, allow_deletions=True).paths == ("src/a.py",)


def test_symlink_is_rejected_even_inside_scope(attempt):
    base, path = attempt
    (path / "src" / "a.py").unlink()
    (path / "src" / "a.py").symlink_to("/etc/passwd")
    commit(path)
    assert rejected_code(path, base) == "FORBIDDEN_MODE"


def test_whitespace_errors_are_rejected(attempt):
    base, path = attempt
    (path / "src" / "a.py").write_text("x = 2   \n")
    commit(path)
    assert rejected_code(path, base) == "DIFF_CHECK_FAILED"


def test_commit_on_another_base_is_rejected(repo, attempt):
    base, path = attempt
    git(path, "checkout", "-q", "--orphan", "other")
    git(path, "rm", "-rq", "--cached", ".")
    (path / "src" / "a.py").write_text("x = 9\n")
    git(path, "add", "src/a.py")
    git(path, "clean", "-fdq")
    commit(path)
    assert rejected_code(path, base) == "NOT_ONE_COMMIT_ON_BASE"


def test_worktree_can_be_removed(repo, attempt):
    _, path = attempt
    remove_attempt_worktree(repo, path)
    assert not path.exists()


def test_existing_empty_directory_is_not_reused(repo, tmp_path):
    (tmp_path / "attempts" / "a1").mkdir(parents=True)
    with pytest.raises(WorkspaceError):
        create_attempt_worktree(repo, git(repo, "rev-parse", "HEAD"), tmp_path / "attempts", "a1")


def test_hooks_planted_in_the_repository_never_run(repo, tmp_path):
    marker = tmp_path / "hook-ran"
    for name in ("post-checkout", "pre-commit", "post-commit", "reference-transaction"):
        hook = repo / ".git" / "hooks" / name
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
    base = git(repo, "rev-parse", "HEAD")
    path = create_attempt_worktree(repo, base, tmp_path / "attempts", "a1")
    assert not marker.exists()
    assert git(path, "status", "--porcelain") == ""


def test_checkout_that_is_not_clean_is_rejected(repo, tmp_path, monkeypatch):
    import cairnwork.workspace as workspace

    real = workspace.run_git

    def dirty_status(path, *args):
        return " M src/a.py\n" if args[:1] == ("status",) else real(path, *args)

    monkeypatch.setattr(workspace, "run_git", dirty_status)
    with pytest.raises(WorkspaceError):
        create_attempt_worktree(repo, git(repo, "rev-parse", "HEAD"), tmp_path / "attempts", "a1")
