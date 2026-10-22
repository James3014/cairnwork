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
        self.commits = {}

    def issues(self, label):
        return [self.bodies[n] for n, labels in self.labels.items() if label in labels]

    def trust(self, number, label):
        return self.trusted

    def comment(self, number, text):
        self.comments.append((number, text))

    def relabel(self, number, add, remove):
        self.labels[number] = (self.labels[number] - {remove}) | {add}

    def parents(self, commit):
        if commit not in self.commits:
            raise RuntimeError("gh api failed: HTTP 404 Not Found")
        return self.commits[commit]

    def find_pr(self, branch):
        return next(
            (
                f"https://github.com/James3014/demo/pull/{i + 1}"
                for i, pr in enumerate(self.prs)
                if pr[0] == branch
            ),
            "",
        )

    def draft_pr(self, branch, title, body):
        self.prs.append((branch, title, body))
        return f"https://github.com/James3014/demo/pull/{len(self.prs)}"


@pytest.fixture
def setup(repo, tmp_path, monkeypatch):
    commit_change(
        repo,
        {
            "src/calc.py": "x = 1\n",
            "tests/test_calc.py": "def test_x():\n    pass\n",
            "pkg/tests/test_calc.py": "def test_x():\n    pass\n",
        },
    )
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(origin)], check=True)
    clone = tmp_path / "clone"
    # Single-branch mirror (Issue #76): a full clone would pre-fetch the hidden-test branches.
    subprocess.run(
        ["git", "clone", "-q", "--single-branch", "--branch", "main", str(origin), str(clone)],
        check=True,
    )
    git(clone, "config", "user.email", "t@e")  # CI runners have no git identity
    git(clone, "config", "user.name", "t")
    started = []
    monkeypatch.setattr(
        runner, "_verify_origin", lambda clone, repo: f"https://github.com/{repo}.git"
    )
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
    commit_change(setup["origin"].parent / "clone", {"other.txt": "main moved\n"})
    github.bodies[7] = {**github.bodies[7], "title": "fix x (edited title)"}
    again = runner.take(setup["intake"], github, setup["clone"])
    assert again[0]["already"] is True and again[0]["task_id"] == first[0]["task_id"]
    assert github.labels[7] == {runner.RUNNING}
    assert len(setup["started"]) == 1 and len(github.comments) == 1
    base = git(setup["clone"], "rev-parse", "origin/main")
    status = setup["intake"].status(task_id=first[0]["task_id"])
    assert status["status"] == "BOUND"
    request = setup["intake"].load_request(first[0]["task_id"])
    assert request.base_commit == base and request.repo == setup["clone"].resolve()


def test_a_multi_branch_mirror_is_refused_at_startup(setup):
    # A mirror cloned without --single-branch pulls every branch, including the ones that hold
    # deferred hidden tests; the runner must refuse it at startup (Issue #76).
    git(setup["clone"], "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    with pytest.raises(RuntimeError, match="LINEAGE_MIRROR_NOT_SINGLE_BRANCH"):
        runner.take(setup["intake"], FakeGitHub([issue()]), setup["clone"])


def test_a_mirror_holding_an_extra_branch_is_refused(setup):
    # Even with a main-only fetch config, a mirror that already fetched another branch (a full clone
    # later reconfigured) still holds the hidden-test branch; the ref check refuses it (Issue #76).
    head = git(setup["clone"], "rev-parse", "HEAD").strip()
    git(setup["clone"], "update-ref", "refs/remotes/origin/hidden", head)
    with pytest.raises(RuntimeError, match="LINEAGE_MIRROR_NOT_SINGLE_BRANCH"):
        runner.take(setup["intake"], FakeGitHub([issue()]), setup["clone"])


def test_untrusted_or_malformed_issues_are_not_taken(setup):
    github = FakeGitHub([issue()], trusted="AUTHOR_UNTRUSTED")
    assert runner.take(setup["intake"], github, setup["clone"]) == [
        {"issue": 7, "skipped": "AUTHOR_UNTRUSTED"}
    ]
    bad = FakeGitHub([issue(block={**BLOCK, "allowed_files": ["pyproject.toml"]})])
    out = runner.take(setup["intake"], bad, setup["clone"])
    assert "TASK_ALLOWED_FILES_PROTECTED" in out[0]["refused"]
    assert setup["started"] == [] and bad.labels[7] == {runner.NEEDS_FIX}
    assert len(bad.comments) == 1
    tests_listed = FakeGitHub([issue(block={**BLOCK, "allowed_files": ["tests/test_calc.py"]})])
    out = runner.take(setup["intake"], tests_listed, setup["clone"])
    assert "PROTECTED_VERIFIER_INPUT" in out[0]["refused"]


def test_a_verified_task_becomes_a_draft_pr(setup, monkeypatch):
    github = FakeGitHub([issue()])
    taken = runner.take(setup["intake"], github, setup["clone"])[0]
    commit = commit_change(setup["clone"], {"src/calc.py": "x = 2\n"})
    monkeypatch.setattr(Intake, "status", _completed(setup, taken, commit))
    monkeypatch.setattr(runner, "_git", _git_without_credentials)
    out = runner.settle(setup["intake"], github, setup["clone"])
    assert out[0]["pr"].startswith("https://")
    branch, title, body = github.prs[0]
    assert (
        branch == f"cairnwork/7-a{'1' * 16}" and "Refs #7" in body and "NEXUS_CORE_ISSUE: 7" in body
    )
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
            "phase": "FINISHED",
            "detail": "FAILED",
        },
    )
    runner.settle(setup["intake"], github, setup["clone"])
    assert github.labels[7] == {runner.FAILED_LABEL} and github.prs == []
    assert "`FAILED`" in github.comments[-1][1] and "`FINISHED`" in github.comments[-1][1]


def test_a_running_task_is_left_alone(setup, monkeypatch):
    github = FakeGitHub([issue()])
    runner.take(setup["intake"], github, setup["clone"])
    monkeypatch.setattr(Intake, "status", lambda self, **kw: {"status": "DISPATCHED"})
    assert runner.settle(setup["intake"], github, setup["clone"]) == []
    assert github.labels[7] == {runner.RUNNING}


def _git_without_credentials(repo, *args, **_kw):
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


def _completed(setup, taken, commit):
    return lambda self, **kw: {
        "task_id": taken["task_id"],
        "status": "COMPLETED",
        "core_verified": True,
        "attempt_id": "a" + "1" * 16,
        "candidate_commit": commit,
        "detail": "",
    }


def test_a_candidate_outside_the_allowed_files_is_not_pushed(setup, monkeypatch):
    github = FakeGitHub([issue()])
    taken = runner.take(setup["intake"], github, setup["clone"])[0]
    commit = commit_change(setup["clone"], {"src/calc.py": "x = 2\n", "src/other.py": "y\n"})
    monkeypatch.setattr(Intake, "status", _completed(setup, taken, commit))
    monkeypatch.setattr(runner, "_git", _git_without_credentials)
    out = runner.settle(setup["intake"], github, setup["clone"])
    assert "outside the Issue's allowed_files" in out[0]["error"] and github.prs == []


def test_settle_after_a_partial_failure_reuses_the_pr(setup, monkeypatch):
    github = FakeGitHub([issue()])
    taken = runner.take(setup["intake"], github, setup["clone"])[0]
    commit = commit_change(setup["clone"], {"src/calc.py": "x = 2\n"})
    monkeypatch.setattr(Intake, "status", _completed(setup, taken, commit))
    monkeypatch.setattr(runner, "_git", _git_without_credentials)
    github.prs.append((f"cairnwork/7-a{'1' * 16}", "t", "b"))  # opened by a pass that then died
    out = runner.settle(setup["intake"], github, setup["clone"])
    assert out[0]["pr"] == "https://github.com/James3014/demo/pull/1" and len(github.prs) == 1


def test_a_strange_attempt_id_is_never_pushed(setup, monkeypatch):
    github = FakeGitHub([issue()])
    taken = runner.take(setup["intake"], github, setup["clone"])[0]
    commit = commit_change(setup["clone"], {"src/calc.py": "x = 2\n"})
    bad = _completed(setup, taken, commit)
    monkeypatch.setattr(
        Intake, "status", lambda self, **kw: {**bad(self), "attempt_id": "x:refs/heads/main"}
    )
    out = runner.settle(setup["intake"], github, setup["clone"])
    assert "unexpected attempt" in out[0]["error"] and github.prs == []


def test_a_running_issue_without_a_record_is_an_error_not_a_guess(setup):
    github = FakeGitHub([issue()])
    github.labels[7] = {runner.RUNNING}
    out = runner.settle(setup["intake"], github, setup["clone"])
    assert "no Cairnwork task recorded" in out[0]["error"]


def test_a_drained_intake_leaves_the_issue_queued_silently(setup):
    setup["intake"].set_accepting(False)
    github = FakeGitHub([issue()])
    assert runner.take(setup["intake"], github, setup["clone"]) == [
        {"issue": 7, "skipped": "NOT_ACCEPTING"}
    ]
    assert github.labels[7] == {runner.QUEUE} and github.comments == []


def test_only_one_pass_runs_at_a_time(setup, capsys):
    import fcntl

    setup["intake"].prepare()
    fd = os.open(setup["intake"].state / "issues.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        code = runner.main(
            [
                "--state",
                str(setup["intake"].state),
                "--repo",
                "James3014/demo",
                "--clone",
                str(setup["clone"]),
                "--gh",
                "/bin/nexus-agent-gh",
                "run-once",
            ]
        )
    finally:
        os.close(fd)
    assert code == 2 and "BUSY" in capsys.readouterr().out


def test_a_cairnwork_side_fault_leaves_the_issue_queued(setup):
    (setup["intake"].state / "config.json").write_text('{"modle": "x"}')
    github = FakeGitHub([issue()])
    out = runner.take(setup["intake"], github, setup["clone"])
    assert out == [{"issue": 7, "skipped": "BAD_CONFIG"}]
    assert github.labels[7] == {runner.QUEUE} and github.comments == []


def test_a_github_error_while_refusing_does_not_stop_the_pass(setup):
    class Flaky(FakeGitHub):
        def relabel(self, number, add, remove):
            if number == 7:
                raise RuntimeError("gh issue failed: 502")
            super().relabel(number, add, remove)

    github = Flaky([issue(block={**BLOCK, "allowed_files": ["pyproject.toml"]}), issue(number=8)])
    out = runner.take(setup["intake"], github, setup["clone"])
    assert "502" in out[0]["error"] and out[1]["duplicate"] is False


def test_an_issue_with_deferred_tests_binds_to_the_tests_base(setup, monkeypatch):
    clone = setup["clone"]
    base = git(clone, "rev-parse", "HEAD")
    commit_change(clone, {"later.txt": "main moved on\n"})
    git(clone, "push", "-q", "origin", "main")
    test_commit = "c" * 40
    github = FakeGitHub([issue(block={**BLOCK, "test_commit": test_commit})])
    github.commits[test_commit] = [base]
    taken = runner.take(setup["intake"], github, clone)[0]
    request = setup["intake"].load_request(taken["task_id"])
    assert request.test_commit == test_commit and request.base_commit == base
    assert test_commit not in request.instructions and "eia-task" not in request.instructions
    assert base in github.comments[-1][1]
    git(clone, "checkout", "-q", "--detach", base)  # what Cairnwork builds: fix, then tests
    commit_change(clone, {"src/calc.py": "x = 2\n"}, message="fix")
    tests = commit_change(clone, {"tests/test_bug.py": "def test_bug():\n    assert 0\n"})
    git(clone, "checkout", "-q", "main")
    monkeypatch.setattr(runner, "_git", _git_without_credentials)
    monkeypatch.setattr(
        Intake, "load_request", lambda self, tid: _with(request, test_commit=tests, base=base)
    )
    monkeypatch.setattr(Intake, "status", _completed(setup, taken, tests))
    out = runner.settle(setup["intake"], github, clone)
    assert out[0]["pr"].startswith("https://")


def _with(request, test_commit, base):
    import dataclasses

    return dataclasses.replace(request, test_commit=test_commit, base_commit=base)


@pytest.mark.parametrize(
    ("parents", "code"),
    [
        (None, "TASK_TEST_COMMIT_UNKNOWN"),
        ([], "TASK_TEST_COMMIT_SHAPE"),
        (["d" * 40], "TASK_TEST_BASE_NOT_ON_MAIN"),
    ],
)
def test_bad_test_commits_need_a_fix(setup, parents, code):
    github = FakeGitHub([issue(block={**BLOCK, "test_commit": "c" * 40})])
    if parents is not None:
        github.commits["c" * 40] = parents
    out = runner.take(setup["intake"], github, setup["clone"])
    assert code in out[0]["refused"] and github.labels[7] == {runner.NEEDS_FIX}


def test_an_issue_with_a_verifier_under_a_configured_test_root_is_taken(setup):
    intake = setup["intake"]
    (intake.state / "config.json").write_text(json.dumps({"test_roots": ["pkg/tests"]}))
    block = {**BLOCK, "verification": ["python3 -m pytest -q pkg/tests/test_calc.py"]}
    out = runner.take(intake, FakeGitHub([issue(block=block)]), setup["clone"])
    assert out[0]["duplicate"] is False, out
    request = intake.load_request(out[0]["task_id"])  # the runner re-reads it with the same roots
    assert request.test_roots == ("pkg/tests",)
    again = runner.take(intake, FakeGitHub([issue(8)]), setup["clone"])  # default-root verifier
    assert again[0]["refused"].startswith("TASK_VERIFICATION_UNSUPPORTED")


@pytest.mark.parametrize("bad", ["x y", "[a](http://evil)", "@team", "", "a" * 65, "-lead"])
def test_a_malformed_task_id_is_refused_by_open_pr_itself(setup, monkeypatch, bad):
    github = FakeGitHub([issue()])
    taken = runner.take(setup["intake"], github, setup["clone"])[0]
    commit = commit_change(setup["clone"], {"src/calc.py": "x = 2\n"})
    request = setup["intake"].load_request(taken["task_id"])
    status = {**_completed(setup, taken, commit)(None), "task_id": bad}
    # load_request would accept it, so only the TASK_ID check in _open_pr stands in the way
    monkeypatch.setattr(Intake, "load_request", lambda self, tid: request)
    monkeypatch.setattr(runner, "_git", _git_without_credentials)
    with pytest.raises(ValueError, match="unexpected task id"):
        runner._open_pr(setup["intake"], github, setup["clone"], 7, status)
    assert github.prs == []
