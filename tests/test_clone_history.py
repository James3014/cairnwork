import subprocess

from conftest import git

from cairnwork.workspace import (
    Attempt,
    _guarded_refs,
    collect_candidate,
    create_attempt_clone,
)

WORKER = ("-c", "user.email=w@example.invalid", "-c", "user.name=w")


def test_worker_clone_has_only_base_history(repo, tmp_path):
    """When base is not the tip, a later "answer" commit must not be reachable or present
    in the worker's clone (otherwise the worker reads it with git log/show)."""
    base = git(repo, "rev-parse", "HEAD")
    (repo / "src" / "a.py").write_text("x = 999  # the answer\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "answer")
    answer = git(repo, "rev-parse", "HEAD")

    clone = create_attempt_clone(repo, base, tmp_path / "attempts", "a1").path
    assert git(clone, "rev-parse", "HEAD") == base
    assert (clone / "src" / "a.py").read_text() == "x = 1\n"  # base, not the answer
    assert answer not in git(clone, "rev-list", "--all").split()  # answer unreachable
    missing = subprocess.run(
        ["git", "-C", str(clone), "cat-file", "-e", answer], capture_output=True
    )
    assert missing.returncode != 0  # the answer commit object is pruned


def test_restriction_keeps_base_history_and_allows_fetch_back(repo, tmp_path):
    """Restriction must not break the normal path: base stays, the worker can commit, and
    collect_candidate still fetches the new HEAD."""
    base = git(repo, "rev-parse", "HEAD")
    clone = create_attempt_clone(repo, base, tmp_path / "attempts", "a1").path
    assert base in git(clone, "rev-list", "HEAD").split()  # base history intact

    (clone / "src" / "a.py").write_text("x = 2\n")
    git(clone, "add", "-A")
    git(clone, *WORKER, "commit", "-q", "-m", "candidate")
    attempt = Attempt("a1", base, clone, _guarded_refs(repo, "a1"))
    cand = collect_candidate(repo, attempt, ("src/a.py",))
    assert cand.paths == ("src/a.py",)
