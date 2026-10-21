"""The Issue runner refuses a clone whose origin is not --repo (Issue #53)."""

import pytest
import test_issue_runner as shared
from conftest import commit_change, git
from test_issue_runner import FakeGitHub, _completed, issue

import cairnwork.issue_runner as runner
from cairnwork.intake import Intake

setup = shared.setup  # the shared fixture (local clone, fake dispatch)
REAL_VERIFY = runner._verify_origin  # the shared fixture stubs it out


GOOD, FOREIGN = "https://github.com/James3014/demo", "https://github.com/Other/demo"


def _clone_with_origin(tmp_path, urls, pushes=()):
    clone = tmp_path / "mirror"
    git(tmp_path, "init", "-q", str(clone))
    git(clone, "config", "user.email", "t@e")
    git(clone, "config", "user.name", "t")
    for url in urls:
        git(clone, "config", "--add", "remote.origin.url", url)
    for push in pushes:
        git(clone, "config", "--add", "remote.origin.pushurl", push)
    return clone


@pytest.mark.parametrize(
    ("urls", "pushes"),
    [
        ([GOOD], []),
        (["https://github.com/james3014/Demo.git"], []),
        ([GOOD, GOOD + ".git"], [GOOD, GOOD]),
    ],
)
def test_https_origins_of_the_same_repo_pass(tmp_path, urls, pushes):
    clone = _clone_with_origin(tmp_path, urls, pushes)
    assert REAL_VERIFY(clone, "James3014/demo") == "https://github.com/James3014/demo.git"


@pytest.mark.parametrize(
    ("urls", "pushes", "code"),
    [
        ([FOREIGN], [], "LINEAGE_REPOSITORY_MISMATCH"),
        (["https://github.com/James3014/other.git"], [], "LINEAGE_REPOSITORY_MISMATCH"),
        (["https://evil.example/James3014/demo"], [], "LINEAGE_REMOTE_UNKNOWN"),
        (["https://user:pw@github.com/James3014/demo"], [], "LINEAGE_REMOTE_UNKNOWN"),
        ([GOOD + "/extra"], [], "LINEAGE_REMOTE_UNKNOWN"),
        (["/tmp/some/local/clone"], [], "LINEAGE_REMOTE_UNKNOWN"),
        (["git@github.com:James3014/demo.git"], [], "LINEAGE_REMOTE_UNKNOWN"),
        (["ssh://git@github.com/James3014/demo.git"], [], "LINEAGE_REMOTE_UNKNOWN"),
        ([FOREIGN], [GOOD], "LINEAGE_REPOSITORY_MISMATCH"),  # only the push URL looks right
        ([GOOD], [FOREIGN], "LINEAGE_REPOSITORY_MISMATCH"),
        ([GOOD], [GOOD, FOREIGN], "LINEAGE_REPOSITORY_MISMATCH"),  # second pushurl
        ([GOOD, FOREIGN], [], "LINEAGE_REPOSITORY_MISMATCH"),  # second url
        ([GOOD, FOREIGN], [GOOD], "LINEAGE_REPOSITORY_MISMATCH"),  # only --all sees it
        ([GOOD], ["git@evil.example:James3014/demo"], "LINEAGE_REMOTE_UNKNOWN"),
        ([], [], "git remote failed"),
    ],
)
def test_other_origins_are_refused(tmp_path, urls, pushes, code):
    clone = _clone_with_origin(tmp_path, urls, pushes)
    with pytest.raises(RuntimeError, match=code):
        REAL_VERIFY(clone, "James3014/demo")


def test_an_empty_answer_is_refused(monkeypatch):
    monkeypatch.setattr(runner, "_git", lambda *a: "")
    with pytest.raises(RuntimeError, match="LINEAGE_REMOTE_UNKNOWN"):
        REAL_VERIFY(None, "James3014/demo")


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
    git(setup["clone"], "remote", "set-url", "origin", "https://github.com/Other/demo.git")
    pushes = []
    real_git = runner._git
    monkeypatch.setattr(
        runner, "_git", lambda r, *a: pushes.append(a) if "push" in a else real_git(r, *a)
    )
    out = runner.settle(setup["intake"], github, setup["clone"])
    assert "LINEAGE_REPOSITORY_MISMATCH" in out[0]["error"]
    assert pushes == [] and github.prs == []


def test_the_push_goes_to_the_verified_url_not_the_remote_name(setup, monkeypatch):
    github = FakeGitHub([issue()])
    taken = runner.take(setup["intake"], github, setup["clone"])[0]
    commit = commit_change(setup["clone"], {"src/calc.py": "x = 2\n"})
    monkeypatch.setattr(Intake, "status", _completed(setup, taken, commit))
    monkeypatch.setattr(runner, "_verify_origin", REAL_VERIFY)
    git(setup["clone"], "remote", "set-url", "origin", "https://github.com/james3014/Demo.git")
    pushes = []
    real_git = runner._git
    monkeypatch.setattr(
        runner, "_git", lambda r, *a: pushes.append(a) if "push" in a else real_git(r, *a)
    )
    runner.settle(setup["intake"], github, setup["clone"])
    assert len(pushes) == 1 and "origin" not in pushes[0]
    assert pushes[0][-2] == "https://github.com/James3014/demo.git"
