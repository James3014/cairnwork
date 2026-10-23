"""The GitHub CLI client the Issue runner drives.

Split out of issue_runner.py to keep that file under the 400-line cap (#164 and #168 were each
under 400 but summed to 410 once merged). Behaviour is unchanged: every call shells out to the
pinned ``gh`` wrapper with a fixed argv and no shell.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any

from cairnwork.issue_task import queue_trusted

REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class GitHub:
    def __init__(self, gh: str, repo: str) -> None:
        if not REPO.fullmatch(repo):
            raise ValueError(f"repo must be OWNER/NAME, got {repo!r}")
        self.gh, self.repo = gh, repo

    def _run(self, *args: str, stdin: str | None = None) -> str:
        done = subprocess.run(  # noqa: S603 - fixed binary, argv list, no shell
            [self.gh, *args], input=stdin, capture_output=True, text=True, timeout=120, check=False
        )
        if done.returncode != 0:
            raise RuntimeError(f"gh {args[0]} failed: {done.stderr.strip()[-300:]}")
        return done.stdout

    def issues(self, label: str) -> list[dict[str, Any]]:
        out = self._run(
            "issue", "list", "-R", self.repo, "--state", "open", "--label", label,
            "--limit", "50", "--json", "number,title,body",
        )  # fmt: skip
        return json.loads(out)

    def trust(self, number: int, label: str) -> str:
        try:
            return self._trust(number, label)
        except (TypeError, AttributeError, KeyError, ValueError):  # odd GitHub output: refuse
            return "TRUST_CHECK_FAILED"

    def current(self, number: int) -> dict[str, Any]:
        out = self._run("issue", "view", str(number), "-R", self.repo, "--json", "body,title")
        return json.loads(out)

    def _trust(self, number: int, label: str) -> str:
        jq = (
            '.[] | select(.event=="labeled" or .event=="renamed") | '
            "{event, label: .label.name, actor: .actor.login, at: .created_at}"
        )
        out = self._run(
            "api", "--paginate", f"repos/{self.repo}/issues/{number}/timeline", "--jq", jq
        )
        events = [json.loads(line) for line in out.splitlines() if line.strip()]
        owner, name = self.repo.split("/")
        query = (
            f'query($num:Int!){{repository(owner:"{owner}",name:"{name}"){{issue(number:$num)'
            "{author{login __typename} userContentEdits(first:100)"
            "{totalCount nodes{editedAt editor{login __typename}}}}}}"
        )
        data = json.loads(
            self._run("api", "graphql", "-f", f"query={query}", "-F", f"num={number}")
        )
        issue = (((data or {}).get("data") or {}).get("repository") or {}).get("issue") or {}
        return queue_trusted(
            label=label, events=events, author=issue.get("author"),
            edits=issue.get("userContentEdits"),
        )  # fmt: skip

    def comment(self, number: int, text: str) -> None:
        self._run("issue", "comment", str(number), "-R", self.repo, "--body-file", "-", stdin=text)

    def relabel(self, number: int, add: str, remove: str) -> None:
        self._run(
            "issue", "edit", str(number), "-R", self.repo, "--add-label", add,
            "--remove-label", remove,
        )  # fmt: skip

    def parents(self, commit: str) -> list[str]:
        out = self._run(
            "api", f"repos/{self.repo}/commits/{commit}", "--jq", ".parents[].sha"
        )  # fmt: skip
        return out.split()

    def find_pr(self, branch: str) -> str:
        out = self._run(
            "pr", "list", "-R", self.repo, "--head", branch, "--state", "all",
            "--json", "url", "--jq", ".[0].url // empty",
        )  # fmt: skip
        return out.strip()

    def draft_pr(self, branch: str, title: str, body: str) -> str:
        return self._run(
            "pr", "create", "-R", self.repo, "--draft", "--base", "main", "--head", branch,
            "--title", title, "--body-file", "-", stdin=body,
        ).strip()  # fmt: skip
