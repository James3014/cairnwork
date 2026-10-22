"""run_git failure detail and the per-call timeout (Issue #145)."""

import subprocess

import pytest
from conftest import commit_change
from test_issue_runner import FakeGitHub, _completed, _git_without_credentials, issue, setup  # noqa: F401

import cairnwork.gitcmd as gitcmd
import cairnwork.issue_runner as runner
from cairnwork.gitcmd import NETWORK_TIMEOUT_SECONDS, GitCommandError, run_git, stderr_tail
from cairnwork.intake import Intake


def test_a_failing_push_reports_gits_stderr(repo, tmp_path):
    with pytest.raises(GitCommandError) as caught:
        run_git(repo, "push", str(tmp_path / "no-such-remote"), "HEAD:refs/heads/x")
    text = str(caught.value)
    assert text.startswith("GIT_COMMAND_FAILED: git push failed: ")
    assert "no-such-remote" in text and "\n" not in text


def test_a_failure_without_stderr_keeps_the_short_message(monkeypatch, tmp_path):
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired("git", 1)

    monkeypatch.setattr(gitcmd.subprocess, "run", boom)
    with pytest.raises(GitCommandError) as caught:
        run_git(tmp_path, "fetch")
    assert str(caught.value) == "GIT_COMMAND_FAILED: git fetch failed"


def test_stderr_tail_is_bounded_and_drops_credentials_and_control_characters():
    noisy = "x" * 5000 + "\nfatal: unable to access 'https://u:ghp_SECRET@github.com/o/r.git/'\n"
    tail = stderr_tail(noisy.encode())
    assert len(tail) <= gitcmd.STDERR_TAIL_CHARS and tail.endswith("r.git/'")
    assert "ghp_SECRET" not in tail and "u:" not in tail
    assert stderr_tail("a\x1b[31mred\x1b[0m\rb") == "a[31mred[0mb"
    assert stderr_tail(None) == ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("fatal: https://user:p@ss@host/x failed", "fatal: https://host/x failed"),
        ("fatal: https://tok:ab/cd@host/x failed", "fatal: https://host/x failed"),
        ("fatal: https://github.com/o/r.git failed", "fatal: https://github.com/o/r.git failed"),
    ],
)
def test_stderr_tail_strips_the_whole_userinfo_and_keeps_plain_urls(raw, expected):
    assert stderr_tail(raw) == expected


@pytest.mark.parametrize("split", ["\r", "\x0b", "\x85", "\n", "\u202e"])
def test_stderr_tail_redacts_userinfo_split_by_a_control_character(split):
    tail = stderr_tail(f"fatal: https://u:p{split}@host/x failed")
    assert "u:p" not in tail and "@" not in tail and "host/x failed" in tail


def test_the_error_names_the_subcommand_not_a_leading_c_override(monkeypatch, tmp_path):
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired("git", 1)

    monkeypatch.setattr(gitcmd.subprocess, "run", boom)
    with pytest.raises(GitCommandError) as caught:
        run_git(tmp_path, "-c", "x=y", "--no-pager", "push")
    assert str(caught.value) == "GIT_COMMAND_FAILED: git push failed"


def test_stderr_tail_drops_bidi_and_line_separator_characters():
    raw = "a\u202eb\u202ac\u2066d\u2069e\u2028f\u2029g"
    assert stderr_tail(raw) == "abcdefg"


@pytest.mark.parametrize("bad", [None, 0, -1, -0.5])
def test_run_git_rejects_a_missing_or_non_positive_timeout(repo, bad):
    with pytest.raises(ValueError, match="timeout"):
        run_git(repo, "rev-parse", "HEAD", timeout=bad)


def _record_timeouts(monkeypatch):
    seen = []
    real = subprocess.run

    def spy(argv, **kw):
        seen.append(([a for a in argv if a in ("fetch", "push", "rev-parse")], kw["timeout"]))
        return real(argv, **kw)

    monkeypatch.setattr(gitcmd.subprocess, "run", spy)
    return seen


def test_run_git_timeout_defaults_short_and_can_be_raised(repo, monkeypatch):
    seen = _record_timeouts(monkeypatch)
    run_git(repo, "rev-parse", "HEAD")
    run_git(repo, "rev-parse", "HEAD", timeout=NETWORK_TIMEOUT_SECONDS)
    assert [t for _, t in seen] == [gitcmd.GIT_TIMEOUT_SECONDS, NETWORK_TIMEOUT_SECONDS]
    assert NETWORK_TIMEOUT_SECONDS == 300 and gitcmd.GIT_TIMEOUT_SECONDS == 60


def test_the_runner_gives_only_fetch_and_push_the_long_timeout(setup, monkeypatch):  # noqa: F811
    seen = []
    real = runner.run_git

    def spy(repo, *args, **kw):
        args = list(args)
        while args and args[0] == "-c":  # the bot credential options: no gh in tests
            args = args[2:]
        seen.append((args[0], kw.get("timeout")))
        return real(repo, *args, **kw)

    monkeypatch.setattr(runner, "run_git", spy)
    github = FakeGitHub([issue()])
    taken = runner.take(setup["intake"], github, setup["clone"])[0]
    commit = commit_change(setup["clone"], {"src/calc.py": "x = 2\n"})
    monkeypatch.setattr(Intake, "status", _completed(setup, taken, commit))
    runner.settle(setup["intake"], github, setup["clone"])
    network = {(c, t) for c, t in seen if c in ("fetch", "push")}
    assert network == {("fetch", NETWORK_TIMEOUT_SECONDS), ("push", NETWORK_TIMEOUT_SECONDS)}
    assert all(t is None for c, t in seen if c not in ("fetch", "push"))
