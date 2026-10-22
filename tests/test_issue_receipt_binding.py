"""PRESERVED #15 at publication: _open_pr pushes only what was notarized (#61)."""
# ruff: noqa: F811

from conftest import commit_change, git, tamper_stored_request
from issue_helpers import notarize
from test_issue_runner import (  # noqa: F401
    FakeGitHub,
    _completed,
    _git_without_credentials,
    issue,
    setup,
)

import cairnwork.issue_runner as runner
from cairnwork.intake import Intake


def test_a_candidate_that_is_not_the_notarized_one_is_not_pushed(setup, monkeypatch):
    github = FakeGitHub([issue()])
    taken = runner.take(setup["intake"], github, setup["clone"])[0]
    notarized = commit_change(setup["clone"], {"src/calc.py": "x = 2\n"})
    notarize(setup, taken, notarized)
    other = commit_change(setup["clone"], {"src/calc.py": "x = 3\n"})
    monkeypatch.setattr(Intake, "status", _completed(setup, taken, other))
    monkeypatch.setattr(runner, "_git", _git_without_credentials)
    out = runner.settle(setup["intake"], github, setup["clone"])
    assert "RECEIPT_BINDING_MISMATCH" in out[0]["error"] and github.prs == []
    assert github.labels[7] == {runner.RUNNING}  # the Issue stays as it was
    assert git(setup["origin"], "for-each-ref", "refs/heads/cairnwork") == ""


def test_a_stored_request_that_differs_from_git_is_not_pushed(setup, monkeypatch):
    github = FakeGitHub([issue()])
    taken = runner.take(setup["intake"], github, setup["clone"])[0]
    commit = commit_change(setup["clone"], {"src/calc.py": "x = 2\n"})
    notarize(setup, taken, commit)
    tamper_stored_request(
        setup["intake"].state / "notary.sqlite",
        lambda r: r["change_set"].__setitem__("diff_hash", "sha256:" + "0" * 64),
    )
    monkeypatch.setattr(Intake, "status", _completed(setup, taken, commit))
    monkeypatch.setattr(runner, "_git", _git_without_credentials)
    out = runner.settle(setup["intake"], github, setup["clone"])
    assert "RECEIPT_BINDING_MISMATCH" in out[0]["error"] and github.prs == []
    assert git(setup["origin"], "for-each-ref", "refs/heads/cairnwork") == ""
