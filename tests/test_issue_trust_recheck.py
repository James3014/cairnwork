"""Trust is re-checked right before dispatch, and odd GitHub output fails closed (PRESERVED #2)."""

# ruff: noqa: F811

import json

import pytest
from conftest import commit_change  # noqa: F401
from test_issue_runner import FakeGitHub, issue, setup  # noqa: F401

import cairnwork.issue_runner as runner
from cairnwork.intake import Intake


class Counting(FakeGitHub):
    """trusted may be a list: one answer per trust call (the last repeats)."""

    calls = 0

    def trust(self, number, label):
        self.calls += 1
        if isinstance(self.trusted, list):
            return self.trusted[min(self.calls, len(self.trusted)) - 1]
        return self.trusted


def _submitted(setup, monkeypatch):
    calls = []
    real = Intake.submit
    monkeypatch.setattr(Intake, "submit", lambda self, p: calls.append(p) or real(self, p))
    return calls


def test_a_stranger_author_is_not_touched_at_all(setup, monkeypatch):
    calls = _submitted(setup, monkeypatch)
    github = FakeGitHub([issue()], trusted="AUTHOR_UNTRUSTED")
    github.comment = github.relabel = lambda *a: pytest.fail("a write reached GitHub")
    assert runner.take(setup["intake"], github, setup["clone"]) == [
        {"issue": 7, "skipped": "AUTHOR_UNTRUSTED"}
    ]
    assert calls == [] and github.labels[7] == {runner.QUEUE}


def test_a_body_edit_after_the_first_check_stops_dispatch(setup, monkeypatch):
    calls = _submitted(setup, monkeypatch)

    class Edits(FakeGitHub):
        def trust(self, number, label):  # the edit lands right after the first check
            self.bodies[number] = {**self.bodies[number], "body": "changed"}
            return ""

    github = Edits([issue()])
    out = runner.take(setup["intake"], github, setup["clone"])
    assert out == [{"issue": 7, "skipped": "TRUST_CHANGED_BEFORE_DISPATCH"}]
    assert calls == [] and github.comments == [] and github.labels[7] == {runner.QUEUE}


def test_a_title_edit_after_the_first_check_stops_dispatch(setup, monkeypatch):
    calls = _submitted(setup, monkeypatch)

    class Edits(FakeGitHub):
        def trust(self, number, label):
            self.bodies[number] = {**self.bodies[number], "title": "other"}
            return ""

    github = Edits([issue()])
    out = runner.take(setup["intake"], github, setup["clone"])
    assert out == [{"issue": 7, "skipped": "TRUST_CHANGED_BEFORE_DISPATCH"}]
    assert calls == [] and github.comments == []


def test_distrust_at_the_second_check_stops_dispatch(setup, monkeypatch):
    calls = _submitted(setup, monkeypatch)
    github = Counting([issue()], trusted=["", "QUEUE_LABELED_BY_UNTRUSTED"])
    out = runner.take(setup["intake"], github, setup["clone"])
    assert out == [{"issue": 7, "skipped": "TRUST_CHANGED_BEFORE_DISPATCH"}]
    assert github.calls == 2 and calls == []
    assert github.comments == [] and github.labels[7] == {runner.QUEUE}


def test_an_unchanged_trusted_issue_is_still_dispatched(setup, monkeypatch):
    calls = _submitted(setup, monkeypatch)
    github = Counting([issue()])
    assert runner.take(setup["intake"], github, setup["clone"])[0]["duplicate"] is False
    assert len(calls) == 1 and github.calls == 2


@pytest.mark.parametrize("timeline", ["[]", '"x"', "7"])
def test_odd_timeline_output_fails_that_issue_closed_and_the_pass_goes_on(
    setup, monkeypatch, timeline
):
    calls = _submitted(setup, monkeypatch)

    class Odd(runner.GitHub):
        def __init__(self, issues):
            super().__init__("/bin/false", "James3014/demo")
            self.rows, self.comments = issues, []
            self.labels = {i["number"]: {runner.QUEUE} for i in issues}

        def issues(self, label):
            return self.rows

        def _run(self, *args, stdin=None):
            if args[0] == "api" and "timeline" in args[2]:  # only issue 7 has a broken timeline
                return timeline if "/7/" in args[2] else json.dumps(GOOD_EVENT)
            if args[0] == "api":
                return json.dumps(GRAPHQL)
            if args[0] == "issue" and args[1] == "view":
                number = int(args[2])
                row = next(i for i in self.rows if i["number"] == number)
                return json.dumps({k: row[k] for k in ("title", "body")})
            if args[0] == "issue" and args[1] == "edit":
                self.labels[int(args[2])] = {runner.RUNNING}
                return ""
            if args[0] == "issue" and args[1] == "comment":
                self.comments.append(args[2])
                return ""
            raise AssertionError(args)

    github = Odd([issue(7), issue(8)])
    out = runner.take(setup["intake"], github, setup["clone"])
    assert out[0] == {"issue": 7, "skipped": "TRUST_CHECK_FAILED"}
    assert out[1]["issue"] == 8 and out[1]["duplicate"] is False
    assert len(calls) == 1 and github.comments == ["8"]
    assert github.labels[7] == {runner.QUEUE}


GOOD_EVENT = {"event": "labeled", "label": "cairnwork:queue", "actor": "James3014",
              "at": "2026-10-01T00:00:00Z"}  # fmt: skip
GRAPHQL = {"data": {"repository": {"issue": {
    "author": {"login": "James3014", "__typename": "User"},
    "userContentEdits": {"totalCount": 0, "nodes": []},
}}}}  # fmt: skip
