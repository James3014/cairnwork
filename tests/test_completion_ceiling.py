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

ROOT = Path(__file__).resolve().parent.parent
# Everything that ships or runs outside tests: the core, the operator scripts, the experimental
# capabilities. tests/ is not scanned (its fixtures name these words on purpose).
SHIPPED = [ROOT / "src", ROOT / "scripts", ROOT / "experimental"]
# A quoted gh/git word that merges, approves, reviews or enables auto-merge, or an API path for one;
# plus the "--auto-merge" family.
FORBIDDEN = re.compile(
    r"""["'](merge|approve|review|ready|--admin|--merge|--squash|--rebase)["']"""
    r"""|--auto\b|\bpr\s+(merge|review|ready)\b|/merge\b|/reviews\b|auto_merge|autoMerge""",
    re.IGNORECASE,
)
# A second way to push main: a quoted refs/heads/main or a quoted HEAD:main / <sha>:main /
# <src>:refs/heads/main refspec. Only counts within PUSH_WINDOW lines after a quoted "push", so the
# fetch-refspec checks that legitimately name refs/heads/main are not flagged.
PUSH_MAIN = re.compile(r"""["']refs/heads/main["']|["'][^"'\s]*:(refs/heads/)?main["']""")
PUSH_WORD = re.compile(r"""["']push["']""")
PUSH_WINDOW = 6
REFSPEC = re.compile(r"[0-9a-f]{40}:refs/heads/cairnwork/\d+-[A-Za-z0-9_-]+")


def forbidden_uses(*roots: Path) -> list[str]:
    """Lines under the roots that name a merge/approve/auto-merge/push-main spelling.

    Scans src/, scripts/ and experimental/ (experimental capabilities ship too); only tests/ is left
    out, because its fixtures name these words on purpose.

    This guards accidental regressions only. Deliberate evasion (splitting a word across string
    pieces, building it at runtime) is out of scope; the runtime tests below are the main guard.
    """
    found = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            lines = path.read_text().splitlines()
            for n, line in enumerate(lines):
                near = lines[max(0, n - PUSH_WINDOW) : n + 1]
                pushes_main = PUSH_MAIN.search(line) and any(PUSH_WORD.search(x) for x in near)
                if FORBIDDEN.search(line) or pushes_main:
                    found.append(f"{path.name}:{n + 1}: {line.strip()}")
    return found


def test_no_source_file_can_merge_approve_or_enable_auto_merge():
    assert [r.name for r in SHIPPED] == ["src", "scripts", "experimental"]
    for root in SHIPPED:
        assert list(root.rglob("*.py")), f"{root} moved; the guard would pass on nothing"
    assert forbidden_uses(*SHIPPED) == []


def test_the_scan_catches_a_merge_call(tmp_path):
    for bad in ('gh("pr", "merge", "7")', 'argv = ["gh", "pr", "merge"]', "# gh pr merge 7",
                'run("--auto")', 'run("approve")', 'api("repos/x/pulls/1/reviews")',
                'api("repos/x/pulls/1/merge")', 'run("--auto-merge")', "run(['--auto'])",
                'git("push", "origin", "refs/heads/main")', 'git("push", "origin", "HEAD:main")',
                "git('push', 'origin', 'HEAD:refs/heads/main')", 'git("push", "o", f"{sha}:main")',
                'git("push", "o", "+x:main")'):  # fmt: skip
        (tmp_path / "m.py").write_text(bad + "\n")
        assert forbidden_uses(tmp_path), bad
    fine_lines = (
        'git("merge-base", "--is-ancestor", a, b)',
        'gh("pr", "create", "--base", "main")',
        'git("push", "origin", f"{sha}:refs/heads/cairnwork/7-a1")',
        'if spec.split(":")[0] != "refs/heads/main":',  # a fetch-refspec check, not a push
        'gh("pr", "create", "--automatic-x")',
    )
    for fine in fine_lines:
        (tmp_path / "m.py").write_text(fine + "\n")
        assert forbidden_uses(tmp_path) == [], fine  # read-only or an issue branch, on purpose


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
