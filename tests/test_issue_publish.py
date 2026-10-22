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
from cairnwork.issue_publish import refusal_code, require_bot_gh


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


def _wrapper(tmp_path, body, name="nexus-agent-gh"):
    path = tmp_path / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return str(path)


def _main(setup, gh):
    argv = ["--state", str(setup["intake"].state), "--repo", "James3014/demo"]
    return runner.main([*argv, "--clone", str(setup["clone"]), "--gh", gh, "run-once"])


def test_a_gh_that_is_not_the_bot_wrapper_is_refused(setup, capsys, monkeypatch):
    monkeypatch.setattr(runner, "require_bot_gh", require_bot_gh)  # the fixture stubs it out
    assert _main(setup, "/usr/bin/gh") == 3
    assert "GH_IDENTITY_NOT_BOT" in capsys.readouterr().out
    assert runner.DEFAULT_GH.endswith("/nexus-agent-gh")  # the default passes the name check


def test_a_wrapper_that_resolves_to_a_personal_user_is_refused(
    setup, tmp_path, capsys, monkeypatch
):
    # Right name, wrong identity: a personal token gets 403 on /installation/repositories.
    monkeypatch.setattr(runner, "require_bot_gh", require_bot_gh)
    gh = _wrapper(
        tmp_path, 'echo "HTTP 403: Resource not accessible by personal access token" >&2; exit 1'
    )
    assert _main(setup, gh) == 3
    assert "GH_IDENTITY_NOT_BOT" in capsys.readouterr().out
    assert not (setup["intake"].state / "issues.lock").exists()  # refused before any state


@pytest.mark.parametrize("out", ["", "James3014/other\n", "notJames3014/demo\n"])
def test_a_probe_that_does_not_list_the_repo_is_refused(setup, tmp_path, out):
    gh = _wrapper(tmp_path, f"printf '{out}'")
    with pytest.raises(RuntimeError, match="GH_IDENTITY_NOT_BOT"):
        require_bot_gh(gh, "James3014/demo", setup["clone"])


def test_a_wrapper_that_cannot_run_is_refused(setup, tmp_path):
    gh = str(tmp_path / "nexus-agent-gh")  # does not exist
    with pytest.raises(RuntimeError, match="GH_IDENTITY_NOT_BOT"):
        require_bot_gh(gh, "James3014/demo", setup["clone"])


def test_the_installation_token_that_lists_the_repo_passes(setup, tmp_path):
    gh = _wrapper(tmp_path, 'echo "James3014/other"; echo "James3014/demo"')
    require_bot_gh(gh, "James3014/demo", setup["clone"])
    args = _wrapper(
        tmp_path,
        '[ "$1 $2" = "api /installation/repositories" ] && echo James3014/demo',
        "nexus-agent-gh",
    )
    require_bot_gh(args, "James3014/demo", setup["clone"])  # it asks the installation endpoint


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


def _settle_with(setup, monkeypatch, commit, **changes):
    github = FakeGitHub([issue()])
    taken = runner.take(setup["intake"], github, setup["clone"])[0]
    commit = commit_change(setup["clone"], {"src/calc.py": "x = 2\n"})
    done = _completed(setup, taken, commit)
    monkeypatch.setattr(Intake, "status", lambda self, **kw: {**done(self), **changes})
    monkeypatch.setattr(runner, "_git", _git_without_credentials)
    return github, runner.settle(setup["intake"], github, setup["clone"])


@pytest.mark.parametrize(
    "url",
    ["@someone fixed", "http://github.com/James3014/demo/pull/1", "https://evil.example/pull/1",
     "https://github.com/other/repo/pull/1", "https://github.com/James3014/demo/pull/1\n@x",
     "https://github.com/James3014/demo/issues/1", ""],
)  # fmt: skip
def test_a_gh_stdout_that_is_not_a_pr_url_is_not_posted(setup, monkeypatch, url):
    github = FakeGitHub([issue()])
    monkeypatch.setattr(github, "draft_pr", lambda *a: url)
    taken = runner.take(setup["intake"], github, setup["clone"])[0]
    commit = commit_change(setup["clone"], {"src/calc.py": "x = 2\n"})
    monkeypatch.setattr(Intake, "status", _completed(setup, taken, commit))
    monkeypatch.setattr(runner, "_git", _git_without_credentials)
    runner.settle(setup["intake"], github, setup["clone"])
    text = github.comments[-1][1]
    assert "draft PR 已開" in text and "someone" not in text and "evil" not in text
    assert "http" not in text


def test_a_real_pr_url_is_posted(setup, monkeypatch):
    github, _ = _settle_with(setup, monkeypatch, None)
    assert "https://github.com/James3014/demo/pull/1" in github.comments[-1][1]


@pytest.mark.parametrize(
    "task_id", ["t`x`", "t @someone", "fixes #1", "t\n# head", "[a](http://x)", "", "t" * 65]
)
def test_a_task_id_that_is_not_plain_never_reaches_the_pr_body(setup, monkeypatch, task_id):
    github, out = _settle_with(setup, monkeypatch, None, task_id=task_id)
    assert github.prs == [] and "error" in out[0]


@pytest.mark.parametrize("code", ["A" * 65, "ÄÖ", "٣٣", "lower", "A B", "A\n", "", 5, None])
def test_an_oversized_or_unicode_refusal_code_becomes_refused(code):
    exc = runner.IssueRefused("X", "d")
    exc.code = code
    assert refusal_code(exc) == "REFUSED"
    exc.code = "A" * 64
    assert refusal_code(exc) == "A" * 64


def test_the_refusal_comment_has_balanced_backticks():
    github = FakeGitHub([issue()])
    runner._needs_fix(github, 7, runner.IssueRefused("TASK_BAD", "d"))
    text = github.comments[-1][1]
    assert text.count("`") % 2 == 0 and "```" not in text


def test_a_probe_that_lists_the_repo_but_exits_nonzero_is_refused(setup, tmp_path):
    gh = _wrapper(tmp_path, "echo James3014/demo; exit 1")
    with pytest.raises(RuntimeError, match="GH_IDENTITY_NOT_BOT"):
        require_bot_gh(gh, "James3014/demo", setup["clone"])
