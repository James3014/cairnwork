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
from cairnwork.changeset import ChangeSetError
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


def test_a_git_failure_in_one_task_does_not_stop_the_others(setup, monkeypatch):
    github = FakeGitHub([issue(7), issue(8)])
    taken = {t["issue"]: t for t in runner.take(setup["intake"], github, setup["clone"])}
    commits = {}
    for number, value in ((7, "2"), (8, "3")):
        commits[number] = commit_change(setup["clone"], {"src/calc.py": f"x = {value}\n"})
        notarize(setup, taken[number], commits[number])
    by_task = {taken[n]["task_id"]: commits[n] for n in taken}
    status = _completed(setup, taken[7], commits[7])
    monkeypatch.setattr(
        Intake,
        "status",
        lambda self, *, task_id: {
            **status(self),
            "task_id": task_id,
            "candidate_commit": by_task[task_id],
        },  # fmt: skip
    )
    real = runner.read_change_set
    seen = []

    def read(repo, base, commit):
        seen.append(commit)
        if len(seen) == 1:
            raise ChangeSetError("git diff-tree failed")
        return real(repo, base, commit)

    monkeypatch.setattr(runner, "read_change_set", read)
    monkeypatch.setattr(runner, "_git", _git_without_credentials)
    out = runner.settle(setup["intake"], github, setup["clone"])
    assert len(out) == 2 and "ChangeSetError" in out[0]["error"] and "pr" in out[1]
    assert len(github.prs) == 1 and len(seen) == 2  # the failing task pushed nothing
    refs = git(setup["origin"], "for-each-ref", "refs/heads/cairnwork")
    assert refs.count("\n") == 0 and refs.strip() != ""
