import pytest
from conftest import git

from cairnwork.workspace import (
    Attempt,
    CandidateRejected,
    WorkspaceError,
    _guarded_refs,
    collect_candidate,
    create_attempt_clone,
    remove_attempt_clone,
)

ALLOWED = ("src/a.py",)
WORKER = ("-c", "user.email=w@example.invalid", "-c", "user.name=w")


@pytest.fixture
def attempt(repo, tmp_path):
    base = git(repo, "rev-parse", "HEAD")
    root = tmp_path / "attempts"
    made = create_attempt_clone(repo, base, root, "a1")
    return base, root, made.path


def commit(path, message="candidate"):
    git(path, "add", "-A")
    git(path, *WORKER, "commit", "-q", "-m", message)
    return git(path, "rev-parse", "HEAD")


_ATTEMPTS = {}


def collect(repo, clone, base, **kwargs):
    made = _ATTEMPTS.get(clone) or Attempt("a1", base, clone, _guarded_refs(repo, "a1"))
    return collect_candidate(repo, made, ALLOWED, **kwargs)


def rejected_code(repo, clone, base, **kwargs):
    with pytest.raises(CandidateRejected) as info:
        collect(repo, clone, base, **kwargs)
    return info.value.code


def test_clone_is_a_detached_checkout_of_base_with_its_own_git_dir(repo, attempt):
    base, _, clone = attempt
    assert git(clone, "rev-parse", "HEAD") == base
    assert (clone / ".git").is_dir()
    assert git(clone, "rev-parse", "--git-common-dir") == ".git"


def test_attempt_path_is_never_reused(repo, attempt):
    base, root, _ = attempt
    with pytest.raises(WorkspaceError):
        create_attempt_clone(repo, base, root, "a1")


def test_existing_empty_directory_is_not_reused(repo, tmp_path):
    (tmp_path / "attempts" / "a1").mkdir(parents=True)
    with pytest.raises(WorkspaceError):
        create_attempt_clone(repo, git(repo, "rev-parse", "HEAD"), tmp_path / "attempts", "a1")


@pytest.mark.parametrize(
    "attempt_id", ["", "a/b", "..", ".", "-x", "a:b", "a*", "a..b", "a.lock", "a b", None]
)
def test_attempt_id_must_be_one_path_component(repo, tmp_path, attempt_id):
    with pytest.raises(WorkspaceError):
        create_attempt_clone(repo, git(repo, "rev-parse", "HEAD"), tmp_path, attempt_id)


@pytest.mark.parametrize("base", ["HEAD", "abc", "f" * 40])
def test_base_must_be_an_existing_full_commit_id(repo, tmp_path, base):
    with pytest.raises(WorkspaceError):
        create_attempt_clone(repo, base, tmp_path, "a1")


def test_hooks_in_cairnworks_repository_never_run(repo, tmp_path):
    marker = tmp_path / "hook-ran"
    for name in ("post-checkout", "reference-transaction", "pre-auto-gc"):
        hook = repo / ".git" / "hooks" / name
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
    base = git(repo, "rev-parse", "HEAD")
    clone = create_attempt_clone(repo, base, tmp_path / "attempts", "a1").path
    (clone / "src" / "a.py").write_text("x = 2\n")
    commit(clone)
    collect(repo, clone, base)  # the fetch into Cairnwork's repo runs reference-transaction
    assert not marker.exists()


def test_clone_shares_no_object_files_with_cairnworks_repository(repo, attempt):
    _, _, clone = attempt
    ours = {p.stat().st_ino for p in (repo / ".git" / "objects").rglob("*") if p.is_file()}
    theirs = {p.stat().st_ino for p in (clone / ".git" / "objects").rglob("*") if p.is_file()}
    assert theirs and not ours & theirs
    assert not (clone / ".git" / "objects" / "info" / "alternates").exists()


def test_one_in_scope_commit_is_accepted(repo, attempt):
    base, _, clone = attempt
    (clone / "src" / "a.py").write_text("x = 2\n")
    head = commit(clone)
    candidate = collect(repo, clone, base)
    assert candidate.candidate_commit == head
    assert candidate.paths == ("src/a.py",)
    assert git(repo, "rev-parse", "refs/cairnwork/attempts/a1") == head


def test_no_commit_is_rejected(repo, attempt):
    base, _, clone = attempt
    assert rejected_code(repo, clone, base) == "NO_COMMIT"


def test_two_commits_are_rejected(repo, attempt):
    base, _, clone = attempt
    (clone / "src" / "a.py").write_text("x = 2\n")
    commit(clone)
    (clone / "src" / "a.py").write_text("x = 3\n")
    commit(clone)
    assert rejected_code(repo, clone, base) == "NOT_ONE_COMMIT_ON_BASE"


def test_out_of_scope_change_is_rejected(repo, attempt):
    base, _, clone = attempt
    (clone / "src" / "b.py").write_text("y = 2\n")
    commit(clone)
    assert rejected_code(repo, clone, base) == "OUT_OF_SCOPE"


def test_deletion_needs_permission(repo, attempt):
    base, _, clone = attempt
    (clone / "src" / "a.py").unlink()
    commit(clone)
    assert rejected_code(repo, clone, base) == "DELETION_FORBIDDEN"
    assert collect(repo, clone, base, allow_deletions=True).paths == ("src/a.py",)


def test_symlink_is_rejected_even_inside_scope(repo, attempt):
    base, _, clone = attempt
    (clone / "src" / "a.py").unlink()
    (clone / "src" / "a.py").symlink_to("/etc/passwd")
    commit(clone)
    assert rejected_code(repo, clone, base) == "FORBIDDEN_MODE"


def test_whitespace_errors_are_rejected(repo, attempt):
    base, _, clone = attempt
    (clone / "src" / "a.py").write_text("x = 2   \n")
    commit(clone)
    assert rejected_code(repo, clone, base) == "DIFF_CHECK_FAILED"


@pytest.mark.parametrize(
    "leftover",
    ["untracked", "ignored", "skip-worktree", "assume-unchanged", "chmod"],
)
def test_any_difference_between_clone_and_candidate_is_rejected(repo, attempt, leftover):
    base, _, clone = attempt
    (clone / "src" / "a.py").write_text("x = 2\n")
    commit(clone)
    if leftover == "untracked":
        (clone / "notes.txt").write_text("left\n")
    elif leftover == "ignored":
        (clone / ".git" / "info" / "exclude").write_text("secret.txt\n")
        (clone / "secret.txt").write_text("hidden\n")
    elif leftover in ("skip-worktree", "assume-unchanged"):
        git(clone, "update-index", f"--{leftover}", "src/b.py")
        (clone / "src" / "b.py").write_text("y = 99\n")
    else:
        (clone / "src" / "b.py").chmod(0o755)
    assert rejected_code(repo, clone, base) == "DIRTY_TREE"


def test_replace_refs_cannot_disguise_the_candidate(repo, attempt):
    base, _, clone = attempt
    (clone / "src" / "a.py").write_text("x = 2\n")
    c1 = commit(clone)
    (clone / "b.txt").write_text("out of scope\n")
    c2 = commit(clone)
    tree = git(clone, "rev-parse", f"{c1}^{{tree}}")
    fake = git(clone, *WORKER, "commit-tree", tree, "-p", base, "-m", "fake")
    git(clone, "replace", c2, fake)
    git(repo, "fetch", "-q", str(clone), "refs/replace/*:refs/replace/*")
    assert rejected_code(repo, clone, base) == "NOT_ONE_COMMIT_ON_BASE"


def test_worker_config_commands_never_run(repo, attempt, tmp_path):
    base, _, clone = attempt
    marker = tmp_path / "worker-code-ran"
    git(clone, "config", "core.fsmonitor", f"touch {marker}")
    git(clone, "config", "filter.evil.clean", f"touch {marker}; cat")
    (clone / ".git" / "info" / "attributes").write_text("* filter=evil\n")
    for name in ("post-checkout", "pre-commit", "reference-transaction"):
        hook = clone / ".git" / "hooks" / name
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
    (clone / "src" / "a.py").write_text("x = 2\n")
    git(clone, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "add", "-A")
    git(
        clone,
        *WORKER,
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "commit",
        "-q",
        "-m",
        "c",
    )
    marker.unlink(missing_ok=True)
    collect(repo, clone, base)
    assert not marker.exists()


def test_worker_cannot_move_cairnworks_refs(repo, attempt):
    base, _, clone = attempt
    before = git(repo, "for-each-ref")
    (clone / "src" / "a.py").write_text("x = 2\n")
    head = commit(clone)
    git(clone, "update-ref", "refs/heads/main", head)
    git(clone, "tag", "planted")
    collect(repo, clone, base)
    after = git(repo, "for-each-ref")
    assert [
        line for line in after.splitlines() if "refs/cairnwork/" not in line
    ] == before.splitlines()


def test_clone_can_be_removed_even_if_the_worker_locked_things(repo, attempt):
    _, root, clone = attempt
    (clone / ".git" / "index.lock").write_text("")
    remove_attempt_clone(root, "a1")
    assert not clone.exists()


def test_remove_refuses_a_symlinked_attempt(tmp_path):
    (tmp_path / "attempts").mkdir()
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "attempts" / "a1").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(WorkspaceError):
        remove_attempt_clone(tmp_path / "attempts", "a1")
    assert (tmp_path / "elsewhere").exists()


def test_clone_has_no_remote_back_to_cairnworks_repository(attempt):
    _, _, clone = attempt
    assert git(clone, "remote") == ""


def test_refs_pushed_into_cairnworks_repository_reject_the_candidate(repo, tmp_path):
    base = git(repo, "rev-parse", "HEAD")
    made = create_attempt_clone(repo, base, tmp_path / "attempts", "a1")
    (made.path / "src" / "a.py").write_text("x = 2\n")
    commit(made.path)
    git(made.path, "push", "-q", str(repo), "HEAD:refs/replace/" + base)
    with pytest.raises(CandidateRejected) as info:
        collect_candidate(repo, made, ALLOWED)
    assert info.value.code == "REPOSITORY_REFS_CHANGED"


def test_another_attempts_ref_changing_rejects_the_candidate(repo, tmp_path):
    base = git(repo, "rev-parse", "HEAD")
    made = create_attempt_clone(repo, base, tmp_path / "attempts", "a1")
    (made.path / "src" / "a.py").write_text("x = 2\n")
    head = commit(made.path)
    git(repo, "update-ref", "refs/cairnwork/attempts/other", base)
    made_other_ref_first = _guarded_refs(repo, "a1")
    other = git(repo, "commit-tree", f"{base}^{{tree}}", "-p", base, "-m", "other")
    git(repo, "update-ref", "refs/cairnwork/attempts/other", other)
    made = Attempt(made.attempt_id, made.base_commit, made.path, made_other_ref_first)
    with pytest.raises(CandidateRejected) as info:
        collect_candidate(repo, made, ALLOWED)
    assert info.value.code == "REPOSITORY_REFS_CHANGED"
    assert head


def test_ordinary_branch_updates_by_cairnwork_are_allowed(repo, tmp_path):
    base = git(repo, "rev-parse", "HEAD")
    made = create_attempt_clone(repo, base, tmp_path / "attempts", "a1")
    (made.path / "src" / "a.py").write_text("x = 2\n")
    head = commit(made.path)
    git(repo, "commit", "-q", "--allow-empty", "-m", "main moved on")
    git(repo, "tag", "release")
    assert collect_candidate(repo, made, ALLOWED).candidate_commit == head


def test_gitlink_paths_must_stay_empty(repo, tmp_path):
    sub = git(repo, "rev-parse", "HEAD")
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{sub},vendor/lib")
    git(repo, "commit", "-q", "-m", "add gitlink")
    base = git(repo, "rev-parse", "HEAD")
    made = create_attempt_clone(repo, base, tmp_path / "attempts", "a1")
    (made.path / "src" / "a.py").write_text("x = 2\n")
    commit(made.path)
    (made.path / "vendor" / "lib").mkdir(parents=True, exist_ok=True)
    (made.path / "vendor" / "lib" / "leftover.py").write_text("x\n")
    with pytest.raises(CandidateRejected) as info:
        collect_candidate(repo, made, ALLOWED)
    assert info.value.code == "DIRTY_TREE"


def test_extra_empty_directory_is_a_difference(repo, attempt):
    base, _, clone = attempt
    (clone / "src" / "a.py").write_text("x = 2\n")
    commit(clone)
    (clone / "src" / "empty").mkdir()
    assert rejected_code(repo, clone, base) == "DIRTY_TREE"


@pytest.mark.parametrize("tracked", [True, False])
def test_unreadable_files_are_rejected_with_a_code(repo, attempt, tracked):
    base, _, clone = attempt
    (clone / "src" / "a.py").write_text("x = 2\n")
    commit(clone)
    target = clone / "src" / ("b.py" if tracked else "extra.bin")
    if not tracked:
        target.write_bytes(b"x" * 10)
    target.chmod(0)
    try:
        assert rejected_code(repo, clone, base) == "DIRTY_TREE"
    finally:
        target.chmod(0o644)


def test_unreadable_directory_cannot_hide_leftovers(repo, attempt):
    base, _, clone = attempt
    (clone / "src" / "a.py").write_text("x = 2\n")
    commit(clone)
    hidden = clone / "hidden"
    hidden.mkdir()
    (hidden / "leftover.txt").write_text("x\n")
    hidden.chmod(0)
    try:
        assert rejected_code(repo, clone, base) == "DIRTY_TREE"
    finally:
        hidden.chmod(0o755)


def test_nested_git_entries_are_leftovers(repo, attempt):
    base, _, clone = attempt
    (clone / "src" / "a.py").write_text("x = 2\n")
    commit(clone)
    (clone / "src" / ".git").write_text("gitdir: /elsewhere\n")
    assert rejected_code(repo, clone, base) == "DIRTY_TREE"


def test_worker_pack_objects_hook_does_not_run_on_fetch(repo, attempt, tmp_path):
    base, _, clone = attempt
    marker = tmp_path / "pack-hook-ran"
    git(clone, "config", "uploadpack.packObjectsHook", f"touch {marker};")
    (clone / "src" / "a.py").write_text("x = 2\n")
    commit(clone)
    collect(repo, clone, base)
    assert not marker.exists()


def test_clone_with_unwritable_directories_can_still_be_removed(repo, attempt):
    _, root, clone = attempt
    locked = clone / "locked" / "deeper"
    locked.mkdir(parents=True)
    (locked / "f").write_text("x\n")
    locked.chmod(0)
    (clone / "locked").chmod(0o500)
    remove_attempt_clone(root, "a1")
    assert not clone.exists()


def test_concurrent_attempts_on_one_base_do_not_reject_each_other(repo, tmp_path):
    base = git(repo, "rev-parse", "HEAD")
    first = create_attempt_clone(repo, base, tmp_path / "attempts", "a1")
    second = create_attempt_clone(repo, base, tmp_path / "attempts", "a2")
    for made, text in ((first, "x = 2\n"), (second, "x = 3\n")):
        (made.path / "src" / "a.py").write_text(text)
        commit(made.path)
    assert collect_candidate(repo, second, ALLOWED)
    assert collect_candidate(repo, first, ALLOWED)


def test_deleted_attempt_ref_rejects_the_candidate(repo, tmp_path):
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/cairnwork/attempts/old", base)
    made = create_attempt_clone(repo, base, tmp_path / "attempts", "a1")
    (made.path / "src" / "a.py").write_text("x = 2\n")
    commit(made.path)
    git(repo, "update-ref", "-d", "refs/cairnwork/attempts/old")
    with pytest.raises(CandidateRejected) as info:
        collect_candidate(repo, made, ALLOWED)
    assert info.value.code == "REPOSITORY_REFS_CHANGED"


def test_attempt_id_with_a_trailing_newline_is_refused(tmp_path):
    from cairnwork.workspace import WorkspaceError, remove_attempt_clone

    with pytest.raises(WorkspaceError, match="must match"):
        remove_attempt_clone(tmp_path, "a1\n")


def test_attempt_base_with_a_trailing_newline_is_refused(repo, tmp_path):
    from cairnwork.workspace import WorkspaceError, create_attempt_clone

    base = git(repo, "rev-parse", "HEAD")
    with pytest.raises(WorkspaceError, match="40-hex"):
        create_attempt_clone(repo, base + "\n", tmp_path / "runs", "a1")
