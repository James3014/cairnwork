"""A task's base must be in origin/main's history (PRESERVED #10, Issue #59)."""

import dataclasses

import pytest
import test_issue_runner as shared
from conftest import commit_change, git
from test_issue_runner import FakeGitHub, _completed, issue

import cairnwork.issue_runner as runner
import cairnwork.lineage as lineage
from cairnwork.intake import Intake

setup = shared.setup


def _side_commit(setup):
    """A commit on a branch that is not main and not pushed anywhere."""
    clone = setup["clone"]
    git(clone, "checkout", "-q", "-b", "side")
    sha = commit_change(clone, {"side.txt": "x\n"})
    git(clone, "checkout", "-q", "main")
    git(clone, "branch", "-q", "-D", "side")  # the mirror must stay single-branch
    return sha


def test_the_tip_of_main_passes(setup):
    tip = git(setup["clone"], "rev-parse", "origin/main")
    lineage.require_on_main(setup["clone"], tip)


def test_an_ancestor_of_main_passes(setup):
    clone = setup["clone"]
    old = git(clone, "rev-parse", "origin/main")
    commit_change(setup["origin"].parent / "repo", {"newer.txt": "n\n"})
    git(setup["origin"].parent / "repo", "push", "-q", str(setup["origin"]), "main")
    lineage.require_on_main(clone, old)  # fetches: main moved, old is still its ancestor


def test_a_commit_only_on_another_branch_is_refused(setup):
    with pytest.raises(RuntimeError, match="LINEAGE_BASE_NOT_ON_MAIN"):
        lineage.require_on_main(setup["clone"], _side_commit(setup))


def test_a_local_commit_ahead_of_origin_main_is_refused(setup):
    local = commit_change(setup["clone"], {"ahead.txt": "x\n"})
    with pytest.raises(RuntimeError, match="LINEAGE_BASE_NOT_ON_MAIN"):
        lineage.require_on_main(setup["clone"], local)


@pytest.mark.parametrize("base", ["f" * 40, "HEAD^{tree}", "origin/main^{tree}", "HEAD", "A" * 40])
def test_an_unknown_or_non_commit_object_is_refused(setup, base):
    with pytest.raises(RuntimeError, match="LINEAGE_BASE_NOT_COMMIT"):
        lineage.require_on_main(setup["clone"], base)


def test_main_is_fetched_first_unless_the_caller_just_did(setup):
    repo = setup["origin"].parent / "repo"
    new = commit_change(repo, {"newer.txt": "n\n"})
    git(repo, "push", "-q", str(setup["origin"]), "main")
    with pytest.raises(RuntimeError, match="LINEAGE_BASE_NOT_COMMIT"):  # not fetched yet
        lineage.require_on_main(setup["clone"], new, fetch=False)
    lineage.require_on_main(setup["clone"], new)


def _settle_with_base(setup, monkeypatch, base):
    github = FakeGitHub([issue()])
    taken = runner.take(setup["intake"], github, setup["clone"])[0]
    commit = commit_change(setup["clone"], {"src/calc.py": "x = 2\n"})
    monkeypatch.setattr(Intake, "status", _completed(setup, taken, commit))
    real = Intake.load_request
    monkeypatch.setattr(
        Intake,
        "load_request",
        lambda self, task_id: dataclasses.replace(real(self, task_id), base_commit=base),
    )
    pushes = []
    real_git = runner._git
    monkeypatch.setattr(
        runner, "_git", lambda r, *a: pushes.append(a) if "push" in a else real_git(r, *a)
    )
    return runner.settle(setup["intake"], github, setup["clone"]), pushes, github


def test_settle_refuses_a_base_that_is_not_on_main_before_pushing(setup, monkeypatch):
    # a submit-path task whose base is a side-branch commit (the candidate descends from it)
    base = _side_commit(setup)
    out, pushes, github = _settle_with_base(setup, monkeypatch, base)
    assert "LINEAGE_BASE_NOT_ON_MAIN" in out[0]["error"]
    assert pushes == [] and github.prs == []


def test_settle_with_a_base_on_main_opens_the_pr(setup, monkeypatch):
    base = git(setup["clone"], "rev-parse", "origin/main")
    out, pushes, github = _settle_with_base(setup, monkeypatch, base)
    assert "LINEAGE" not in str(out[0])
    assert len(pushes) == 1 and len(github.prs) == 1  # the PR was actually opened


def test_a_failed_fetch_is_its_own_error(setup):
    git(setup["clone"], "remote", "set-url", "origin", str(setup["clone"] / "nowhere"))
    with pytest.raises(RuntimeError, match="LINEAGE_FETCH_FAILED"):
        lineage.require_on_main(setup["clone"], "a" * 40)


def test_the_fetch_updates_the_tracking_ref(setup):
    repo = setup["origin"].parent / "repo"
    new = commit_change(repo, {"newer.txt": "n\n"})
    git(repo, "push", "-q", str(setup["origin"]), "main")
    lineage.require_on_main(setup["clone"], new)
    assert git(setup["clone"], "rev-parse", "refs/remotes/origin/main") == new


def test_an_uppercase_git_suffix_is_normalised(tmp_path):
    clone = tmp_path / "c"
    git(tmp_path, "init", "-q", str(clone))
    git(clone, "remote", "add", "origin", "https://github.com/James3014/Demo.GIT")
    lineage.verify_origin(clone, "James3014/demo")


def test_env_pushinsteadof_cannot_split_checker_and_pusher(setup, monkeypatch):
    # GIT_CONFIG_* would redirect a push made with the full environment; the runner drops it
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "url.https://evil.example/x/.pushInsteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/James3014/")
    clone = setup["clone"]
    git(clone, "remote", "set-url", "origin", "https://github.com/James3014/demo.git")
    lineage.verify_origin(clone, "James3014/demo")
    out = runner._git(clone, "remote", "get-url", "--push", "origin").strip()
    assert out == "https://github.com/James3014/demo.git"


def test_git_dir_in_the_environment_is_ignored(setup, monkeypatch, tmp_path):
    other = tmp_path / "other"
    git(tmp_path, "init", "-q", str(other))
    git(other, "remote", "add", "origin", "https://evil.example/James3014/demo.git")
    git(setup["clone"], "remote", "set-url", "origin", "https://github.com/James3014/demo.git")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))  # after the test's own git calls
    lineage.verify_origin(setup["clone"], "James3014/demo")
    assert "github.com" in runner._git(setup["clone"], "remote", "get-url", "origin")
