import subprocess

import pytest
from conftest import git

from cairnwork.gitcmd import GitCommandError, run_git
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


def test_worker_clone_cannot_read_a_tagged_answer_blob(repo, tmp_path):
    """Adversarial: a blob holding the answer, referenced only by a tag (no commit on any
    branch), must also be pruned — the check covers every object type, not just commits."""
    base = git(repo, "rev-parse", "HEAD")
    (repo / "secret.txt").write_text("the answer is 42\n")
    git(repo, "add", "secret.txt")
    git(repo, "commit", "-q", "-m", "answer")
    blob = git(repo, "rev-parse", "HEAD:secret.txt")
    git(repo, "tag", "answer-tag", blob)  # a tag straight to the blob
    git(repo, "reset", "--hard", base)  # base is the tip again; only the tag reaches the blob

    clone = create_attempt_clone(repo, base, tmp_path / "attempts", "a1").path
    missing = subprocess.run(["git", "-C", str(clone), "cat-file", "-e", blob], capture_output=True)
    assert missing.returncode != 0  # the tagged answer blob is gone


def test_run_git_isolate_config_ignores_operator_global_config(repo, tmp_path, monkeypatch):
    """isolate_config must ignore ~/.gitconfig, where a gc.recentObjectsHook command would
    live (core.hooksPath does not cover it, and stripping GIT_* does not stop git reading
    the default global config)."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[cairnworktest]\n\tval = fromglobal\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)

    assert run_git(repo, "config", "--get", "cairnworktest.val").strip() == "fromglobal"
    with pytest.raises(GitCommandError):
        run_git(repo, "config", "--get", "cairnworktest.val", isolate_config=True)
