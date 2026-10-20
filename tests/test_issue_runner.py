"""The Issue runner with a fake GitHub and a real local clone (no network)."""

import json
import os
import subprocess

import pytest
from conftest import commit_change, git

import cairnwork.intake as intake_module
import cairnwork.issue_runner as runner
from cairnwork.intake import Intake

BLOCK = {
    "difficulty": "easy",
    "allowed_files": ["src/calc.py"],
    "verification": ["python3 -m pytest -q tests/test_calc.py"],
    "capability_ids": ["x"],
}


class FakeGitHub(runner.GitHub):
    def __init__(self, issues, trusted=""):
        super().__init__("/bin/false", "James3014/demo")
        self.labels = {i["number"]: {runner.QUEUE} for i in issues}
        self.bodies = {i["number"]: i for i in issues}
        self.trusted = trusted
        self.comments, self.prs = [], []

    def issues(self, label):
        return [self.bodies[n] for n, labels in self.labels.items() if label in labels]

    def trust(self, number, label):
        return self.trusted

    def comment(self, number, text):
        self.comments.append((number, text))

    def relabel(self, number, add, remove):
        self.labels[number] = (self.labels[number] - {remove}) | {add}

    def draft_pr(self, branch, title, body):
        self.prs.append((branch, title, body))
        return f"https://example.invalid/pr/{len(self.prs)}"


@pytest.fixture
def setup(repo, tmp_path, monkeypatch):
    commit_change(
        repo, {"src/calc.py": "x = 1\n", "tests/test_calc.py": "def test_x():\n    pass\n"}
    )
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(origin)], check=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    started = []
    monkeypatch.setattr(intake_module, "_spawn", lambda argv, **kw: started.append(argv))
    intake = Intake(tmp_path / "state")
    intake.set_accepting(True)
    return {"intake": intake, "clone": clone, "origin": origin, "started": started}


def issue(number=7, block=BLOCK):
    return {"number": number, "title": "fix x", "body": f"```eia-task\n{json.dumps(block)}\n```"}


def test_a_trusted_issue_is_taken_once(setup):
    github = FakeGitHub([issue()])
    first = runner.take(setup["intake"], github, setup["clone"])
    assert first[0]["duplicate"] is False and len(setup["started"]) == 1
    assert github.labels[7] == {runner.RUNNING} and len(github.comments) == 1
    github.labels[7] = {runner.QUEUE}  # e.g. the relabel had failed: the next pass retries
    again = runner.take(setup["intake"], github, setup["clone"])
    assert again[0]["duplicate"] is True and again[0]["task_id"] == first[0]["task_id"]
    assert len(setup["started"]) == 1 and len(github.comments) == 1
    base = git(setup["clone"], "rev-parse", "origin/main")
    status = setup["intake"].status(task_id=first[0]["task_id"])
    assert status["status"] == "BOUND"
    request = setup["intake"].load_request(first[0]["task_id"])
    assert request.base_commit == base and request.repo == setup["clone"].resolve()


def test_untrusted_or_malformed_issues_are_not_taken(setup):
    github = FakeGitHub([issue()], trusted="AUTHOR_UNTRUSTED")
    assert runner.take(setup["intake"], github, setup["clone"]) == [
        {"issue": 7, "skipped": "AUTHOR_UNTRUSTED"}
    ]
    bad = FakeGitHub([issue(block={**BLOCK, "allowed_files": ["pyproject.toml"]})])
    out = runner.take(setup["intake"], bad, setup["clone"])
    assert "TASK_ALLOWED_FILES_PROTECTED" in out[0]["refused"]
    assert setup["started"] == [] and bad.labels[7] == {runner.QUEUE}


def test_a_verified_task_becomes_a_draft_pr(setup, monkeypatch):
    github = FakeGitHub([issue()])
    taken = runner.take(setup["intake"], github, setup["clone"])[0]
    commit = commit_change(setup["clone"], {"src/calc.py": "x = 2\n"})
    monkeypatch.setattr(
        Intake,
        "status",
        lambda self, **kw: {
            "task_id": taken["task_id"],
            "status": "COMPLETED",
            "core_verified": True,
            "attempt_id": "a1",
            "candidate_commit": commit,
            "detail": "",
        },
    )
    monkeypatch.setattr(runner, "_git", _git_without_credentials)
    out = runner.settle(setup["intake"], github, setup["clone"])
    assert out[0]["pr"].startswith("https://")
    branch, title, body = github.prs[0]
    assert branch == "cairnwork/7-a1" and "Refs #7" in body and "NEXUS_CORE_ISSUE: 7" in body
    assert "Closes" not in body and "KEEP_OPEN" in body
    pushed = subprocess.run(
        ["git", "-C", str(setup["origin"]), "rev-parse", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert pushed == commit and github.labels[7] == {runner.DONE}


def test_a_failed_task_is_reported_not_retried(setup, monkeypatch):
    github = FakeGitHub([issue()])
    runner.take(setup["intake"], github, setup["clone"])
    monkeypatch.setattr(
        Intake,
        "status",
        lambda self, **kw: {
            "status": "FAILED",
            "core_verified": False,
            "detail": "CORE_FAILED_VERIFICATION",
        },
    )
    runner.settle(setup["intake"], github, setup["clone"])
    assert github.labels[7] == {runner.FAILED_LABEL} and github.prs == []
    assert "CORE_FAILED_VERIFICATION" in github.comments[-1][1]


def test_a_running_task_is_left_alone(setup, monkeypatch):
    github = FakeGitHub([issue()])
    runner.take(setup["intake"], github, setup["clone"])
    monkeypatch.setattr(Intake, "status", lambda self, **kw: {"status": "DISPATCHED"})
    assert runner.settle(setup["intake"], github, setup["clone"]) == []
    assert github.labels[7] == {runner.RUNNING}


def _git_without_credentials(repo, *args):
    """The real helper with the bot credential options removed (no gh in tests)."""
    args = list(args)
    while args and args[0] == "-c":
        args = args[2:]
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    ).stdout


def test_completed_without_core_verification_opens_no_pr(setup, monkeypatch):
    github = FakeGitHub([issue()])
    runner.take(setup["intake"], github, setup["clone"])
    monkeypatch.setattr(
        Intake,
        "status",
        lambda self, **kw: {"status": "COMPLETED", "core_verified": False, "detail": "x"},
    )
    runner.settle(setup["intake"], github, setup["clone"])
    assert github.prs == [] and github.labels[7] == {runner.FAILED_LABEL}
