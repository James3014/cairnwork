"""``cairnwork-issues``: take queued GitHub Issues straight into Cairnwork.

    cairnwork-issues --state DIR --repo OWNER/NAME --clone PATH [--gh BIN] run-once

One pass does two things:

* **take**: every open Issue labelled ``cairnwork:queue`` whose queueing is trusted
  (issue_task.queue_trusted) and whose ``eia-task`` block parses becomes a task in the
  intake at ``--state`` (the same state the Gateway route uses, so ``in-flight`` sees
  both). Base = ``origin/main`` of the dedicated ``--clone`` (fetched first; never a
  shared working tree). Idempotency key and card hash come from the Issue number and the
  block, so a re-run starts nothing new and an edited block is a new task. The Issue
  gets ``cairnwork:running`` and a comment with the task id.
* **settle**: for every ``cairnwork:running`` Issue, a COMPLETED, Core-verified task is
  pushed as branch ``cairnwork/<issue>-<attempt>`` and opened as a **draft** PR (it is
  reviewed and merged like any other PR; Cairnwork never merges). A FAILED task gets a
  comment with its reason and ``cairnwork:failed``.

GitHub calls go through ``--gh`` (the bot's gh wrapper). Any GitHub or git error leaves
the Issue as it is for the next pass; nothing is guessed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from cairnwork.intake import Intake, IntakeRefused
from cairnwork.issue_task import IssueRefused, parse_task_block, queue_trusted
from cairnwork.ownership import OwnershipUnknown
from cairnwork.pipeline import TaskRejected

QUEUE, RUNNING, FAILED_LABEL, DONE = (
    "cairnwork:queue",
    "cairnwork:running",
    "cairnwork:failed",
    "cairnwork:pr-opened",
)
DEFAULT_GH = str(Path.home() / ".config/nexus-agent-bot/bin/nexus-agent-gh")


class GitHub:
    def __init__(self, gh: str, repo: str) -> None:
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

    def draft_pr(self, branch: str, title: str, body: str) -> str:
        return self._run(
            "pr", "create", "-R", self.repo, "--draft", "--base", "main", "--head", branch,
            "--title", title, "--body-file", "-", stdin=body,
        ).strip()  # fmt: skip


def take(intake: Intake, github: GitHub, clone: Path) -> list[dict[str, Any]]:
    _git(clone, "fetch", "-q", "origin", "main")
    base = _git(clone, "rev-parse", "origin/main").strip()
    results = []
    for issue in github.issues(QUEUE):
        number = int(issue["number"])
        try:
            reason = github.trust(number, QUEUE)
            if reason:
                results.append({"issue": number, "skipped": reason})
                continue
            task = parse_task_block(issue.get("body") or "")
            payload = {
                "idempotency_key": f"gh:{github.repo}#{number}:{task.block_sha256[:16]}",
                "card_hash": f"gh:{github.repo}#{number}:{task.block_sha256}",
                "title": str(issue.get("title") or f"Issue #{number}")[:200],
                "instructions": _instructions(github.repo, number, issue, task.summary),
                "repo": str(clone),
                "base_commit": base,
                "allowed_files": list(task.allowed_files),
                "verifiers": [[f"pytest-{i}", list(v)] for i, v in enumerate(task.verifiers)],
            }
            out = intake.submit(payload)
            _remember(intake, github.repo, number, out["task_id"])
            if not out["duplicate"]:
                github.comment(
                    number, f"Cairnwork 已接手：task `{out['task_id']}`，base `{base}`。"
                )
            github.relabel(number, RUNNING, QUEUE)
            results.append({"issue": number, **out})
        except (IssueRefused, IntakeRefused, TaskRejected) as exc:
            results.append({"issue": number, "refused": str(exc)})
        except (RuntimeError, OSError, ValueError, KeyError) as exc:
            results.append({"issue": number, "error": f"{type(exc).__name__}: {exc}"})
    return results


def settle(intake: Intake, github: GitHub, clone: Path) -> list[dict[str, Any]]:
    results = []
    for issue in github.issues(RUNNING):
        number = int(issue["number"])
        try:
            status = intake.status(task_id=_recall(intake, github.repo, number))
            if status["status"] in ("BOUND", "DISPATCHED"):
                continue
            if status["status"] == "COMPLETED" and status["core_verified"]:
                results.append({"issue": number, "pr": _open_pr(github, clone, number, status)})
                github.relabel(number, DONE, RUNNING)
            else:
                detail = status.get("detail") or status["status"]
                github.comment(number, f"Cairnwork 沒有完成：`{status['status']}`（{detail}）。")
                github.relabel(number, FAILED_LABEL, RUNNING)
                results.append({"issue": number, "status": status["status"]})
        except (OwnershipUnknown, RuntimeError, OSError, ValueError, KeyError) as exc:
            results.append({"issue": number, "error": f"{type(exc).__name__}: {exc}"})
    return results


def _open_pr(github: GitHub, clone: Path, number: int, status: dict[str, Any]) -> str:
    branch = f"cairnwork/{number}-{status['attempt_id']}"
    commit = status["candidate_commit"]
    _git(
        clone, "-c", "credential.helper=", "-c",
        f"credential.helper=!{github.gh} auth git-credential",
        "push", "origin", f"{commit}:refs/heads/{branch}",
    )  # fmt: skip
    body = (
        f"Refs #{number}\n\nCairnwork: 受影響（Cairnwork 試點：Issue 直接送 Cairnwork 修）\n\n"
        f"- Cairnwork task `{status['task_id']}`，attempt `{status['attempt_id']}`\n"
        f"- 候選 commit `{commit}`，Core 判定 VERIFIED（Cairnwork notary）\n"
        "- 這是 draft：照一般規則審查、檢查全綠後再由審查者合併；Cairnwork 不合併。\n\n"
        f"<!-- NEXUS_CORE_ISSUE: {number} -->\n<!-- NEXUS_ISSUE_INTENT_V1\n"
        f'[{{"issue": {number}, "on_merge": "KEEP_OPEN"}}]\nNEXUS_ISSUE_INTENT_V1 -->\n\n'
        "🤖 Generated with [Claude Code](https://claude.com/claude-code)\n"
    )
    title = f"Cairnwork: fix for #{number} (Refs #{number})"
    url = github.draft_pr(branch, title, body)
    github.comment(number, f"Cairnwork 修好並通過 Core 驗證，draft PR：{url}")
    return url


def _instructions(repo: str, number: int, issue: dict[str, Any], summary: str) -> str:
    return (
        f"Fix GitHub Issue {repo}#{number}: {issue.get('title')}\n\n{summary}\n\n"
        f"Issue text (from a trusted author):\n{(issue.get('body') or '')[:6000]}"
    )


def _remember(intake: Intake, repo: str, number: int, task_id: str) -> None:
    path = intake.state / "issues" / f"{repo.replace('/', '__')}__{number}.json"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text(json.dumps({"repo": repo, "issue": number, "task_id": task_id}))


def _recall(intake: Intake, repo: str, number: int) -> str:
    path = intake.state / "issues" / f"{repo.replace('/', '__')}__{number}.json"
    return json.loads(path.read_text())["task_id"]


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603 - fixed binary, argv list, no shell
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=300, check=False
    )
    if done.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {done.stderr.strip()[-300:]}")
    return done.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cairnwork-issues")
    parser.add_argument("--state", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--clone", required=True)
    parser.add_argument("--gh", default=DEFAULT_GH)
    parser.add_argument("command", choices=["run-once"])
    args = parser.parse_args(argv)
    intake = Intake(Path(args.state).resolve())
    github = GitHub(args.gh, args.repo)
    clone = Path(args.clone).resolve()
    result = {"take": take(intake, github, clone), "settle": settle(intake, github, clone)}
    print(json.dumps(result, sort_keys=True))
    errors = [r for part in result.values() for r in part if "error" in r]
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
