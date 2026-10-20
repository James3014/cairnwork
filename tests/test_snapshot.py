import os

import pytest
from conftest import git

from cairnwork.snapshot import snapshot_candidate
from cairnwork.workspace import CandidateRejected, create_attempt_clone

ALLOWED = ("src/a.py",)


@pytest.fixture
def attempt(repo, tmp_path):
    return create_attempt_clone(repo, git(repo, "rev-parse", "HEAD"), tmp_path / "att", "a1")


def code(repo, attempt, allowed=ALLOWED, **kwargs):
    with pytest.raises(CandidateRejected) as info:
        snapshot_candidate(repo, attempt, allowed, **kwargs)
    return info.value.code


def test_edited_allowed_file_becomes_one_commit_on_base(repo, attempt):
    (attempt.path / "src" / "a.py").write_text("x = 2\n")
    candidate = snapshot_candidate(repo, attempt, ALLOWED)
    assert git(repo, "rev-parse", f"{candidate.candidate_commit}^") == attempt.base_commit
    assert git(repo, "show", f"{candidate.candidate_commit}:src/a.py") == "x = 2"
    assert git(repo, "rev-parse", "refs/cairnwork/attempts/a1") == candidate.candidate_commit


def test_executable_bit_is_kept(repo, attempt):
    path = attempt.path / "src" / "a.py"
    path.write_text("x = 2\n")
    path.chmod(0o755)
    candidate = snapshot_candidate(repo, attempt, ALLOWED)
    assert git(repo, "ls-tree", candidate.candidate_commit, "src/a.py").startswith("100755")


def test_no_change_is_rejected(repo, attempt):
    assert code(repo, attempt) == "NO_COMMIT"


def test_edit_outside_allowed_files_is_rejected(repo, attempt):
    (attempt.path / "src" / "a.py").write_text("x = 2\n")
    (attempt.path / "src" / "b.py").write_text("y = 9\n")
    assert code(repo, attempt) == "DIRTY_TREE"


def test_new_untracked_file_is_rejected(repo, attempt):
    (attempt.path / "src" / "a.py").write_text("x = 2\n")
    (attempt.path / "notes.md").write_text("x\n")
    assert code(repo, attempt) == "DIRTY_TREE"


def test_deletion_needs_permission(repo, attempt):
    (attempt.path / "src" / "a.py").unlink()
    assert code(repo, attempt) == "DELETION_FORBIDDEN"


def test_symlinked_allowed_file_is_rejected(repo, attempt, tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("token\n")
    (attempt.path / "src" / "a.py").unlink()
    (attempt.path / "src" / "a.py").symlink_to(secret)
    assert code(repo, attempt) == "FORBIDDEN_MODE"


def test_symlinked_parent_directory_is_rejected(repo, attempt, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.py").write_text("stolen\n")
    src = attempt.path / "src"
    for child in src.iterdir():
        child.unlink()
    src.rmdir()
    src.symlink_to(outside)
    assert code(repo, attempt) == "FORBIDDEN_MODE"
    assert "stolen" not in git(repo, "cat-file", "--batch-all-objects", "--batch-check")


def test_worker_attributes_and_filters_do_not_run(repo, attempt, tmp_path):
    marker = tmp_path / "filter-ran"
    git(attempt.path, "config", "filter.evil.clean", f"touch {marker}; cat")
    (attempt.path / ".git" / "info" / "attributes").write_text("* filter=evil\n")
    (attempt.path / "src" / "a.py").write_text("x = 2\n")
    snapshot_candidate(repo, attempt, ALLOWED)
    assert not marker.exists()


def test_hard_link_to_a_file_outside_the_clone_is_rejected(repo, attempt, tmp_path):
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE KEY\n")
    target = attempt.path / "src" / "a.py"
    target.unlink()
    os.link(secret, target)
    assert code(repo, attempt) == "FORBIDDEN_MODE"
    assert "PRIVATE KEY" not in git(repo, "log", "--all", "-p")


def test_fifo_in_place_of_an_allowed_file_is_rejected(repo, attempt):
    target = attempt.path / "src" / "a.py"
    target.unlink()
    os.mkfifo(target)
    assert code(repo, attempt) == "FORBIDDEN_MODE"


def test_allowed_path_that_is_a_directory_is_forbidden(repo, attempt):
    target = attempt.path / "src" / "a.py"
    target.unlink()
    target.mkdir()
    assert code(repo, attempt) == "FORBIDDEN_MODE"


def test_oversized_allowed_file_is_rejected(repo, attempt, monkeypatch):
    import cairnwork.snapshot as snapshot

    monkeypatch.setattr(snapshot, "MAX_FILE_BYTES", 4)
    (attempt.path / "src" / "a.py").write_text("x = 2222\n")
    assert code(repo, attempt) == "FORBIDDEN_MODE"
