"""PRESERVED #24: what the Issue runner may say on GitHub (comments, PR body, gh identity)."""
# ruff: noqa: F811 (the imported setup fixture is used as a test argument)

import pytest
from conftest import commit_change
from test_issue_runner import (
    FakeGitHub,
    _completed,
    _git_without_credentials,
    issue,
    setup,  # noqa: F401 - pytest fixture, F811 on use
)

import cairnwork.issue_runner as runner
from cairnwork.intake import Intake


def test_a_failure_comment_never_carries_the_detail_or_a_host_path(setup, monkeypatch):
    # Crash right after dispatch, then recover: the last phase is DISPATCHED, whose detail is
    # the absolute run_root. It must not reach a public comment (mutation: post detail -> fails).
    github = FakeGitHub([issue()])
    runner.take(setup["intake"], github, setup["clone"])
    secret = "/private/var/folders/zz/T/cairnwork-run-secret"
    monkeypatch.setattr(
        Intake,
        "status",
        lambda self, **kw: {
            "status": "FAILED", "core_verified": False, "phase": "DISPATCHED", "detail": secret,
        },
    )  # fmt: skip
    runner.settle(setup["intake"], github, setup["clone"])
    text = github.comments[-1][1]
    assert "secret" not in text and "/private" not in text and "`DISPATCHED`" in text


@pytest.mark.parametrize("phase", ["../../etc", "@someone", "```x", "a b"])
def test_an_unknown_phase_or_status_is_not_echoed(setup, monkeypatch, phase):
    github = FakeGitHub([issue()])
    runner.take(setup["intake"], github, setup["clone"])
    monkeypatch.setattr(
        Intake,
        "status",
        lambda self, **kw: {"status": phase, "core_verified": False, "phase": phase},
    )
    runner.settle(setup["intake"], github, setup["clone"])
    text = github.comments[-1][1]
    assert phase not in text and "`UNKNOWN`" in text


def test_a_refusal_comment_carries_only_the_code(setup):
    github = FakeGitHub([issue()])
    evil = "x `y` @someone\n# head"
    exc = runner.IssueRefused("TASK_BAD_THING", evil)
    out = runner._needs_fix(github, 7, exc)
    assert "@someone" not in github.comments[-1][1] and "`TASK_BAD_THING`" in github.comments[-1][1]
    assert evil in out["refused"]  # the detail stays in the local output
    runner._needs_fix(github, 7, ValueError("@someone\n`x`"))  # no code at all
    assert "@someone" not in github.comments[-1][1] and "`REFUSED`" in github.comments[-1][1]


def test_a_gh_that_is_not_the_bot_wrapper_is_refused(setup, capsys):
    argv = ["--state", str(setup["intake"].state), "--repo", "James3014/demo"]
    code = runner.main([*argv, "--clone", str(setup["clone"]), "--gh", "/usr/bin/gh", "run-once"])
    assert code == 3 and "GH_IDENTITY_NOT_BOT" in capsys.readouterr().out
    assert runner.DEFAULT_GH.endswith("/nexus-agent-gh")  # the default passes the check


def test_a_draft_pr_body_has_only_validated_fields(setup, monkeypatch):
    github = FakeGitHub([issue()])
    taken = runner.take(setup["intake"], github, setup["clone"])[0]
    commit = commit_change(setup["clone"], {"src/calc.py": "x = 2\n"})
    monkeypatch.setattr(Intake, "status", _completed(setup, taken, commit))
    monkeypatch.setattr(runner, "_git", _git_without_credentials)
    runner.settle(setup["intake"], github, setup["clone"])
    branch, title, body = github.prs[0]
    assert title == "Cairnwork: fix for #7 (Refs #7)" and "fix x" not in body
    assert commit in body and branch.endswith(f"-a{'1' * 16}")
