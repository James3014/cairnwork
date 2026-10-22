"""PRESERVED #25: Cairnwork never merges, approves or pushes main; it only opens draft PRs."""

import json
import re
import subprocess
from pathlib import Path

import pytest
from conftest import commit_change
from test_agy_worker import TASK, _dispatched, attempt, fake_agy
from test_issue_runner import (
    FakeGitHub,
    _completed,
    _git_without_credentials,
    issue,
    setup,  # noqa: F401 - the shared fixture, reached through rig
)

import cairnwork.issue_runner as runner
from cairnwork.intake import Intake
from cairnwork.workers import agy as agy_module
from cairnwork.workers.agy import AgyWorker

SRC = Path(__file__).resolve().parent.parent / "src"
# A quoted gh/git word that merges, approves, reviews or enables auto-merge, or an API path for one.
FORBIDDEN = re.compile(
    r"""["'](merge|approve|review|ready|--auto|--admin|--merge|--squash|--rebase)["']"""
    r"""|\bpr\s+(merge|review|ready)\b|/merge\b|/reviews\b|auto_merge|autoMerge""",
    re.IGNORECASE,
)
REFSPEC = re.compile(r"[0-9a-f]{40}:refs/heads/cairnwork/\d+-[A-Za-z0-9_-]+")


def forbidden_uses(root: Path) -> list[str]:
    return [
        f"{path.relative_to(root)}:{n}: {line.strip()}"
        for path in sorted(root.rglob("*.py"))
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if FORBIDDEN.search(line)
    ]


def test_no_source_file_can_merge_approve_or_enable_auto_merge():
    assert list(SRC.glob("cairnwork*")), "scan root moved; the guard would pass on nothing"
    assert forbidden_uses(SRC) == []


def test_the_scan_catches_a_merge_call(tmp_path):
    for bad in ('gh("pr", "merge", "7")', 'argv = ["gh", "pr", "merge"]', "# gh pr merge 7",
                'run("--auto")', 'run("approve")', 'api("repos/x/pulls/1/reviews")',
                'api("repos/x/pulls/1/merge")'):  # fmt: skip
        (tmp_path / "m.py").write_text(bad + "\n")
        assert forbidden_uses(tmp_path), bad
    (tmp_path / "m.py").write_text('git("merge-base", "--is-ancestor", a, b)\n')
    assert forbidden_uses(tmp_path) == []  # merge-base is read-only and used on purpose


@pytest.fixture
def rig(request):
    return request.getfixturevalue("setup")


class RecordingGitHub(FakeGitHub):
    """The fake GitHub, but draft_pr is the real one and its gh argv is recorded."""

    def __init__(self, issues):
        super().__init__(issues)
        self.gh_calls = []

    def _run(self, *args, stdin=None):
        self.gh_calls.append(args)
        return "https://github.com/James3014/demo/pull/1\n"

    def draft_pr(self, branch, title, body):
        self.prs.append((branch, title, body))
        return runner.GitHub.draft_pr(self, branch, title, body)


def _round(rig, monkeypatch):
    github = RecordingGitHub([issue()])
    taken = runner.take(rig["intake"], github, rig["clone"])[0]
    commit = commit_change(rig["clone"], {"src/calc.py": "x = 2\n"})
    monkeypatch.setattr(Intake, "status", _completed(rig, taken, commit))
    pushes = []

    def recording_git(repo, *args, **kw):
        stripped = list(args)
        while stripped and stripped[0] == "-c":
            stripped = stripped[2:]
        if "push" in stripped:
            pushes.append(stripped)
        return _git_without_credentials(repo, *args, **kw)

    monkeypatch.setattr(runner, "_git", recording_git)
    assert runner.settle(rig["intake"], github, rig["clone"])[0]["pr"].startswith("https://")
    return github, pushes, commit


def test_a_full_round_opens_exactly_one_draft_pr_against_main(rig, monkeypatch):
    github, _, _ = _round(rig, monkeypatch)
    assert github.gh_calls == [github.gh_calls[0]]  # the only gh call that ran for real
    args = github.gh_calls[0]
    assert args[:2] == ("pr", "create")
    assert "--draft" in args
    assert args[args.index("--base") + 1] == "main"
    assert not {"merge", "review", "ready", "--auto", "--admin"} & set(args)
    assert len(github.prs) == 1 and github.labels[7] == {runner.DONE}


def test_a_full_round_pushes_one_cairnwork_branch_and_never_main(rig, monkeypatch):
    _, pushes, commit = _round(rig, monkeypatch)
    assert len(pushes) == 1
    assert pushes[0][0] == "push" and pushes[0][1] == "origin" and len(pushes[0]) == 3
    assert REFSPEC.fullmatch(pushes[0][2]) and pushes[0][2].startswith(commit)
    main = subprocess.run(
        ["git", "-C", str(rig["origin"]), "rev-parse", "refs/heads/main"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip
    assert main != commit  # the candidate is only on its own branch


@pytest.mark.parametrize(
    "bad",
    [
        f"{'a' * 40}:refs/heads/main",
        f"+{'a' * 40}:refs/heads/cairnwork/7-a1",
        f"{'a' * 40}:refs/heads/cairnwork/7-a1:refs/heads/main",
        "HEAD:refs/heads/cairnwork/7-a1",
        f"{'a' * 40}:refs/heads/cairnwork/main",
        f"{'a' * 40}:refs/heads/cairnwork/7-a1/../../main",
    ],
)
def test_the_refspec_pattern_rejects_anything_but_an_issue_branch(bad):
    assert not REFSPEC.fullmatch(bad)


CREDENTIALS = {
    "GH_TOKEN": "t1",
    "GITHUB_TOKEN": "t2",
    "GH_ENTERPRISE_TOKEN": "t3",
    "NEXUS_BOT_TOKEN": "t4",
    "GIT_ASKPASS": "/bin/echo",
}


def _assert_no_credentials(env):
    leaked = [k for k in env if k in CREDENTIALS or k.endswith("_TOKEN")]
    assert leaked == []  # PRESERVED #25: the worker has no GitHub rights through its environment


def test_agy_environment_carries_no_github_credentials(tmp_path, monkeypatch):
    for key, value in CREDENTIALS.items():
        monkeypatch.setenv(key, value)
    dump = tmp_path / "env.json"
    agy = fake_agy(tmp_path, f"open({str(dump)!r}, 'w').write(json.dumps(dict(os.environ)))")
    workdir = tmp_path / attempt(7)
    workdir.mkdir()
    AgyWorker(agy=agy).run(workdir, TASK)
    _assert_no_credentials(json.loads(dump.read_text()))


def test_dispatcher_environment_carries_no_github_credentials(tmp_path, monkeypatch):
    for key, value in CREDENTIALS.items():
        monkeypatch.setenv(key, value)
    dump = tmp_path / "env.json"
    worker, workdir, _ = _dispatched(
        tmp_path, f"open({str(dump)!r}, 'w').write(json.dumps(dict(os.environ)))"
    )
    worker.run(workdir, TASK)
    _assert_no_credentials(json.loads(dump.read_text()))


def test_the_environment_whitelist_names_no_credential():
    assert not [k for k in agy_module.ENV_KEEP if k in CREDENTIALS or k.endswith("_TOKEN")]
