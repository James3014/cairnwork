"""The Issue runner refuses a clone whose origin is not --repo (Issue #53)."""

import pytest
import test_issue_runner as shared
from conftest import commit_change, git
from test_issue_runner import FakeGitHub, _completed, issue

import cairnwork.issue_runner as runner
from cairnwork.intake import Intake

setup = shared.setup  # the shared fixture (local clone, fake dispatch)
REAL_VERIFY = runner._verify_origin  # the shared fixture stubs it out


def _clone_with_origin(tmp_path, url, push=None):
    clone = tmp_path / "mirror"
    git(tmp_path, "init", "-q", str(clone))
    git(clone, "config", "user.email", "t@e")
    git(clone, "config", "user.name", "t")
    if url:
        git(clone, "remote", "add", "origin", url)
    if push:
        git(clone, "remote", "set-url", "--push", "origin", push)
    return clone


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/James3014/demo",
        "https://github.com/james3014/Demo.git",
        "git@github.com:James3014/demo.git",
        "git@github.com:James3014/demo",
        "ssh://git@github.com/James3014/demo.git",
    ],
)
def test_origin_forms_of_the_same_repo_pass(tmp_path, url):
    REAL_VERIFY(_clone_with_origin(tmp_path, url), "James3014/demo")


@pytest.mark.parametrize(
    ("url", "push", "code"),
    [
        ("https://github.com/Other/demo", None, "LINEAGE_REPOSITORY_MISMATCH"),
        ("https://github.com/James3014/other.git", None, "LINEAGE_REPOSITORY_MISMATCH"),
        ("git@github.com:James3014/other.git", None, "LINEAGE_REPOSITORY_MISMATCH"),
        ("https://evil.example/James3014/demo", None, "LINEAGE_REMOTE_UNKNOWN"),
        ("https://user:pw@github.com/James3014/demo", None, "LINEAGE_REMOTE_UNKNOWN"),
        ("https://github.com/James3014/demo/extra", None, "LINEAGE_REMOTE_UNKNOWN"),
        ("/tmp/some/local/clone", None, "LINEAGE_REMOTE_UNKNOWN"),
        ("https://github.com/James3014/demo", "https://github.com/Other/demo", "MISMATCH"),
        ("https://github.com/James3014/demo", "git@evil.example:James3014/demo", "UNKNOWN"),
        (None, None, "git remote failed"),
    ],
)
def test_other_origins_are_refused(tmp_path, url, push, code):
    clone = _clone_with_origin(tmp_path, url, push)
    with pytest.raises(RuntimeError, match=code):
        REAL_VERIFY(clone, "James3014/demo")


def test_take_with_a_foreign_origin_dispatches_nothing(setup, monkeypatch):
    monkeypatch.setattr(runner, "_verify_origin", REAL_VERIFY)
    git(setup["clone"], "remote", "set-url", "origin", "https://github.com/Other/demo.git")
    github = FakeGitHub([issue()])
    with pytest.raises(RuntimeError, match="LINEAGE_REPOSITORY_MISMATCH"):
        runner.take(setup["intake"], github, setup["clone"])
    assert not setup["started"] and github.comments == []
    assert github.labels[7] == {runner.QUEUE}


def test_main_refuses_a_foreign_origin_before_touching_state(setup, capsys, monkeypatch):
    git(setup["clone"], "remote", "set-url", "origin", "https://github.com/Other/demo.git")
    argv = ["--state", str(setup["intake"].state / "new"), "--repo", "James3014/demo"]
    # main() uses the real check: undo the fixture's no-op for this one call
    runner._verify_origin = REAL_VERIFY
    try:
        code = runner.main(
            [*argv, "--clone", str(setup["clone"]), "--gh", "/bin/false", "run-once"]
        )
    finally:
        runner._verify_origin = lambda clone, repo: None
    out = capsys.readouterr().out
    assert code == 3 and "LINEAGE_REPOSITORY_MISMATCH" in out
    assert not (setup["intake"].state / "new").exists()


def test_a_remote_swapped_after_take_is_not_pushed_to(setup, monkeypatch):
    github = FakeGitHub([issue()])
    taken = runner.take(setup["intake"], github, setup["clone"])[0]
    commit = commit_change(setup["clone"], {"src/calc.py": "x = 2\n"})
    monkeypatch.setattr(Intake, "status", _completed(setup, taken, commit))
    monkeypatch.setattr(runner, "_verify_origin", REAL_VERIFY)
    git(setup["clone"], "remote", "set-url", "origin", "git@github.com:Other/demo.git")
    pushes = []
    real_git = runner._git
    monkeypatch.setattr(
        runner, "_git", lambda r, *a: pushes.append(a) if "push" in a else real_git(r, *a)
    )
    out = runner.settle(setup["intake"], github, setup["clone"])
    assert "LINEAGE_REPOSITORY_MISMATCH" in out[0]["error"]
    assert pushes == [] and github.prs == []
