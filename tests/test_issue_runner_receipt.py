"""#229 regression for the Issue runner: with deferred tests the attempt runs in the state's mirror,
so the PR head is the commit the journal names as CANDIDATE and the one Core notarized, bound to
the commit (no binding). Nothing is monkeypatched between the run and the PR."""
# ruff: noqa: F811

import json
import sqlite3
from dataclasses import replace

import pytest
from conftest import git
from test_issue_runner import BLOCK, FakeGitHub, _git_without_credentials, issue, setup  # noqa: F401
from test_pipeline import EditWorker

import cairnwork.issue_runner as runner
from cairnwork.pipeline import run_bound_task

TEST = (
    "import pathlib\n"
    "def test_x():\n    pass\n"
    "def test_bug():\n"
    "    src = pathlib.Path(__file__).parent.parent / 'src' / 'calc.py'\n"
    "    assert src.read_text() == 'x = 2\\n'\n"
)


@pytest.mark.sandbox
def test_pr_head_is_the_journal_candidate_and_the_notarized_commit(setup, tmp_path, monkeypatch):
    clone, origin, intake = setup["clone"], setup["origin"], setup["intake"]
    base = git(clone, "rev-parse", "HEAD")
    author = tmp_path / "author"  # the tests exist only on the origin, as on GitHub
    git(tmp_path, "clone", "-q", str(origin), str(author))
    git(author, "config", "user.email", "t@e")
    git(author, "config", "user.name", "t")
    (author / "tests/test_calc.py").write_text(TEST)
    git(author, "add", "-A")
    git(author, "commit", "-q", "-m", "tests")
    tests = git(author, "rev-parse", "HEAD")
    git(author, "push", "-q", "origin", f"{tests}:refs/heads/hidden-tests")
    github = FakeGitHub([issue(block={**BLOCK, "test_commit": tests})])
    github.commits[tests] = [base]
    taken = runner.take(intake, github, clone)[0]
    assert "task_id" in taken, taken

    result = run_bound_task(
        replace(intake.load_request(taken["task_id"]), before_attempt=lambda: ""),  # trust holds
        taken["task_id"], ledger=intake.ledger,
        notary=intake.notary, journal=intake.journal, worker=EditWorker({"src/calc.py": "x = 2\n"}),
        run_root=intake.run_root,
    )  # fmt: skip
    assert result.verified, result.reason
    candidates = [e.detail for e in intake.journal.entries(taken["task_id"])
                  if e.phase == "CANDIDATE"]  # fmt: skip
    conn = sqlite3.connect(intake.state / "notary.sqlite")
    notarized, request, binding = conn.execute(
        "SELECT candidate_commit, request_json, binding_json FROM core_verifications"
    ).fetchone()
    conn.close()

    monkeypatch.setattr(runner, "_git", _git_without_credentials)  # no credential helper here
    out = runner.settle(intake, github, clone)
    assert out[0]["pr"].startswith("https://"), out
    branch = github.prs[0][0]
    pr_head = git(origin, "rev-parse", f"refs/heads/{branch}")
    assert pr_head == candidates[-1] == notarized == result.candidate_commit
    assert git(clone, "rev-parse", f"{pr_head}^") != base  # fix, then the tests on top
    assert binding is None
    target = json.loads(request)["change_set"]["target_revision"]
    assert target == f"git-commit:{pr_head}"
