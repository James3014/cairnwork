"""The opt-in post-verified hook: off = nothing changes; a failing hook is never a verdict."""

import inspect
import json
import stat
import subprocess

import pytest
from conftest import commit_change
from issue_helpers import _completed
from test_issue_runner import (
    FakeGitHub,
    _git_without_credentials,
    issue,
    setup,  # noqa: F401 - the shared fixture, reached through rig
)

import cairnwork.intake as intake_module
import cairnwork.intake_config as intake_config
import cairnwork.issue_runner as runner
import cairnwork.post_verified as post_verified
from cairnwork.intake import Intake, IntakeRefused
from cairnwork.issue_publish import done_text

TOKEN = "ghs_secret_token_value"
ALLOWED_ENV = {
    "PATH", "HOME", "LANG", "LC_ALL", "CAIRNWORK_GITHUB_TOKEN", "GIT_TERMINAL_PROMPT",
    "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "GIT_CONFIG_KEY_1",
    "GIT_CONFIG_VALUE_1",
}  # fmt: skip


@pytest.fixture
def rig(request):
    return request.getfixturevalue("setup")


def _hook(tmp_path, body="exit 0", name="hook.sh"):
    """A fake hook: records its argv, cwd and whole environment, then runs body."""
    out = tmp_path / "seen"
    out.mkdir(exist_ok=True)
    path = tmp_path / name
    path.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > {out}/argv\npwd > {out}/cwd\nenv > {out}/env\n{body}\n'
    )
    path.chmod(0o755)
    return str(path), out


def _config(rig, **keys):
    (rig["intake"].state / "config.json").write_text(json.dumps(keys))


def _round(rig, monkeypatch, **keys):
    """Take Issue 7, complete it, settle once; returns (github, settle results)."""
    github = FakeGitHub([issue()])
    taken = runner.take(rig["intake"], github, rig["clone"])[0]
    commit = commit_change(rig["clone"], {"src/calc.py": "x = 2\n"})
    monkeypatch.setattr(Intake, "status", _completed(rig, taken, commit))
    monkeypatch.setattr(runner, "_git", _git_without_credentials)
    if keys:
        _config(rig, **keys)
    return github, runner.settle(rig["intake"], github, rig["clone"]), commit


def _spy(monkeypatch):
    """The hook runs recorded (a call carrying --task-id); every other subprocess is untouched."""
    calls, real = [], subprocess.run

    def spy(argv, *args, **kw):
        if "--task-id" in argv:
            calls.append(argv)
        return real(argv, *args, **kw)

    monkeypatch.setattr(post_verified.subprocess, "run", spy)
    return calls


def _log(rig):
    path = rig["intake"].state / post_verified.LOG_NAME
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_off_by_default_changes_nothing(rig, monkeypatch, capsys):
    calls = _spy(monkeypatch)
    github, out, _ = _round(rig, monkeypatch)
    assert out == [{"issue": 7, "pr": "https://github.com/James3014/demo/pull/1"}]
    assert github.labels[7] == {runner.DONE} and len(github.comments) == 2
    assert calls == [] and not (rig["intake"].state / post_verified.LOG_NAME).exists()
    assert capsys.readouterr().err == ""


def test_the_off_path_never_reaches_the_subprocess(tmp_path):
    cfg = dict(intake_config.DEFAULT_CONFIG)
    assert post_verified.run_post_verified_hook(
        tmp_path, cfg, task_id="t", attempt_id="a", candidate="c", clone=tmp_path,
        repo_slug="o/r", gh="/bin/gh",
    ) is None  # fmt: skip
    assert list(tmp_path.iterdir()) == []


def test_on_and_ok_runs_the_hook_once_with_a_narrow_environment(rig, monkeypatch, tmp_path):
    hook, seen = _hook(tmp_path, f'echo "ok"; echo "{TOKEN}" >&2')
    monkeypatch.setenv("CAIRNWORK_GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("UNRELATED_RUNNER_SECRET", "nope")
    monkeypatch.setenv("GIT_SSH_COMMAND", "false")
    spawned = _spy(monkeypatch)
    github, out, commit = _round(rig, monkeypatch, post_verified_hook=hook)
    assert out == [
        {"issue": 7, "pr": "https://github.com/James3014/demo/pull/1", "post_verified_hook": "OK"}
    ]
    assert len(spawned) == 1 and github.labels[7] == {runner.DONE}
    args = (seen / "argv").read_text().split("\n")[:-1]
    assert args[0::2] == ["--state", "--task-id", "--attempt-id", "--candidate", "--repo",
                          "--repo-slug"]  # fmt: skip
    values = dict(zip(args[0::2], args[1::2], strict=True))
    assert values["--state"] == str(rig["intake"].state) and values["--candidate"] == commit
    assert values["--attempt-id"] == "a" + "1" * 16 and values["--repo-slug"] == "James3014/demo"
    assert values["--repo"] == str(rig["clone"]) and values["--task-id"].startswith("t")
    assert TOKEN not in " ".join(args)  # never an argument
    assert (seen / "cwd").read_text().strip() == "/"
    env = dict(
        line.split("=", 1) for line in (seen / "env").read_text().splitlines() if "=" in line
    )
    assert env["CAIRNWORK_GITHUB_TOKEN"] == TOKEN
    assert env["GIT_CONFIG_COUNT"] == "2" and env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_KEY_0"] == env["GIT_CONFIG_KEY_1"] == "credential.helper"
    assert env["GIT_CONFIG_VALUE_0"] == ""
    assert env["GIT_CONFIG_VALUE_1"] == "!/bin/false auth git-credential"
    assert "UNRELATED_RUNNER_SECRET" not in env and "GIT_SSH_COMMAND" not in env
    # sh adds PWD/SHLVL/_ of its own; everything else must be on the allowed list
    assert set(env) - {"PWD", "SHLVL", "_", "OLDPWD"} <= ALLOWED_ENV
    (line,) = _log(rig)
    assert line["returncode"] == 0 and line["error"] is None and line["stdout_tail"] == "ok\n"
    assert line["candidate"] == commit and line["time"].endswith("+00:00")
    log = rig["intake"].state / post_verified.LOG_NAME
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert TOKEN not in log.read_text()


@pytest.mark.parametrize("failure", ["exit2", "timeout", "missing"])
def test_a_failing_hook_never_changes_the_outcome(rig, monkeypatch, tmp_path, capsys, failure):
    if failure == "exit2":
        hook, _seen = _hook(tmp_path, f'echo "boom {TOKEN}"; exit 2')
        keys = {"post_verified_hook": hook}
    elif failure == "timeout":
        hook, _seen = _hook(tmp_path, "sleep 30")
        keys = {"post_verified_hook": hook, "post_verified_hook_seconds": 1}
    else:
        keys = {"post_verified_hook": str(tmp_path / "no-such-hook")}
    monkeypatch.setenv("CAIRNWORK_GITHUB_TOKEN", TOKEN)
    capsys.readouterr()
    github, out, _ = _round(rig, monkeypatch, **keys)
    err = capsys.readouterr().err
    url = "https://github.com/James3014/demo/pull/1"  # the same as with the hook off
    assert out[0]["pr"] == url and "error" not in out[0]
    assert github.labels == {7: {runner.DONE}} and len(github.prs) == 1
    assert github.comments[-1][1] == done_text(url, "James3014/demo") and len(github.comments) == 2
    reason = {"exit2": "exit 2", "timeout": "TIMEOUT", "missing": "FileNotFoundError"}[failure]
    assert out[0]["post_verified_hook"].startswith(reason)
    assert err.startswith("ALERT post_verified_hook task t") and reason in err
    assert TOKEN not in err
    (line,) = _log(rig)
    assert (line["returncode"] == 2) == (failure == "exit2")
    assert (line["error"] or "").startswith(reason) == (failure != "exit2")
    assert TOKEN not in json.dumps(line)
    assert "[redacted]" in line["stdout_tail"] or failure != "exit2"


def test_the_runner_exit_code_ignores_the_hook(rig, monkeypatch, tmp_path, capsys):
    hook, _seen = _hook(tmp_path, "exit 2")
    github = FakeGitHub([issue()])
    taken = runner.take(rig["intake"], github, rig["clone"])[0]
    commit = commit_change(rig["clone"], {"src/calc.py": "x = 2\n"})
    monkeypatch.setattr(Intake, "status", _completed(rig, taken, commit))
    monkeypatch.setattr(runner, "_git", _git_without_credentials)
    monkeypatch.setattr(runner, "GitHub", lambda gh, repo: github)
    _config(rig, post_verified_hook=hook)
    argv = ["--state", str(rig["intake"].state), "--repo", "James3014/demo",
            "--clone", str(rig["clone"]), "--gh", "/bin/false", "run-once"]  # fmt: skip
    capsys.readouterr()
    assert runner.main(argv) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["settle"][0]["post_verified_hook"] == "exit 2" and "ALERT" in captured.err
    assert github.labels[7] == {runner.DONE}


def test_the_hook_is_not_run_for_a_failed_task(rig, monkeypatch, tmp_path):
    hook, seen = _hook(tmp_path)
    github = FakeGitHub([issue()])
    runner.take(rig["intake"], github, rig["clone"])
    _config(rig, post_verified_hook=hook)
    monkeypatch.setattr(
        Intake, "status",
        lambda self, **kw: {"status": "FAILED", "core_verified": False, "phase": "F", "detail": ""},
    )  # fmt: skip
    out = runner.settle(rig["intake"], github, rig["clone"])
    assert out == [{"issue": 7, "status": "FAILED"}]
    assert not (seen / "argv").exists()
    assert not (rig["intake"].state / post_verified.LOG_NAME).exists()


def test_the_hook_is_not_run_when_the_pr_could_not_be_opened(rig, monkeypatch, tmp_path):
    hook, seen = _hook(tmp_path)
    github = FakeGitHub([issue()])
    taken = runner.take(rig["intake"], github, rig["clone"])[0]
    commit = commit_change(rig["clone"], {"src/calc.py": "x = 2\n", "src/other.py": "y\n"})
    monkeypatch.setattr(Intake, "status", _completed(rig, taken, commit))
    monkeypatch.setattr(runner, "_git", _git_without_credentials)
    _config(rig, post_verified_hook=hook)
    out = runner.settle(rig["intake"], github, rig["clone"])
    assert "allowed_files" in out[0]["error"] and "post_verified_hook" not in out[0]
    assert not (seen / "argv").exists()


def test_an_unwritable_log_does_not_raise_and_is_alerted(tmp_path, capsys):
    hook, _seen = _hook(tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / post_verified.LOG_NAME).symlink_to(tmp_path / "elsewhere")  # NOFOLLOW
    cfg = {**intake_config.DEFAULT_CONFIG, "post_verified_hook": hook}
    done = post_verified.run_post_verified_hook(
        tmp_path / "state", cfg, task_id="t1", attempt_id="a1", candidate="c", clone=tmp_path,
        repo_slug="o/r", gh="/bin/gh",
    )  # fmt: skip
    assert done == {"returncode": 0, "ok": True, "reason": None}
    assert "cannot record" in capsys.readouterr().err and not (tmp_path / "elsewhere").exists()


def test_an_exception_in_the_hook_step_is_not_a_verdict(rig, monkeypatch, capsys):
    def boom(*a, **k):
        raise ValueError("unexpected")

    monkeypatch.setattr(runner, "run_post_verified_hook", boom)
    github, out, _ = _round(rig, monkeypatch)
    assert out == [{"issue": 7, "pr": "https://github.com/James3014/demo/pull/1",
                    "post_verified_hook": "ValueError"}]  # fmt: skip
    assert (
        github.labels[7] == {runner.DONE} and "ALERT post_verified_hook" in capsys.readouterr().err
    )


@pytest.mark.parametrize(
    "keys",
    [
        {"post_verified_hook": "relative/hook.sh"},
        {"post_verified_hook": 7},
        {"post_verified_hook_seconds": "300"},
        {"post_verified_hook_seconds": 0},
        {"post_verified_hook_seconds": True},
    ],
)
def test_bad_hook_config_is_refused(tmp_path, keys):
    intake = Intake(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps(keys))
    with pytest.raises(IntakeRefused) as refused:
        intake.config()
    assert refused.value.code == "BAD_CONFIG"


def test_the_hook_config_defaults_and_good_values(tmp_path):
    intake = Intake(tmp_path)
    assert intake.config()["post_verified_hook"] is None
    assert intake.config()["post_verified_hook_seconds"] == 300
    (tmp_path / "config.json").write_text(
        json.dumps({"post_verified_hook": "/usr/bin/true", "post_verified_hook_seconds": 5})
    )
    assert intake.config()["post_verified_hook"] == "/usr/bin/true"
    assert intake.config()["post_verified_hook_seconds"] == 5


def test_default_config_is_the_same_object_everywhere():
    from cairnwork.intake import DEFAULT_CONFIG

    assert intake_module.DEFAULT_CONFIG is intake_config.DEFAULT_CONFIG is DEFAULT_CONFIG


def test_core_does_not_import_the_attest_package():
    for module in (post_verified, intake_config):
        assert "cairnwork_attest" not in inspect.getsource(module)
