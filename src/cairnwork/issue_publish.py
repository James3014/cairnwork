"""What cairnwork-issues may say on GitHub (PRESERVED #24).

Comments carry only allow-listed words: a fixed status, a phase from the journal's fixed list
and an error *code*. Never ``detail`` (a host path after a crash), exception text, worker output
or Issue text. Those stay in the local JSON output."""

from __future__ import annotations

import re
from pathlib import Path

from cairnwork.journal import PHASES

STATUSES = frozenset({"FAILED", "CANCELLED", "SUPERSEDED", "COMPLETED"})
BOT_GH = "nexus-agent-gh"
REFUSAL_CODE = re.compile(r"[A-Z0-9_]{1,64}")
TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")  # plain text: no markdown, @, #, spaces
PR_URL = r"https://github\.com/{repo}/pull/[0-9]+"


def pr_url(url: str, repo: str) -> str:
    """The PR URL gh printed, if it really is a PR of the configured repo; else empty."""
    return url if re.fullmatch(PR_URL.format(repo=re.escape(repo)), url) else ""


def done_text(url: str, repo: str) -> str:
    safe = pr_url(url, repo)
    tail = f"draft PR：{safe}" if safe else "draft PR 已開，連結在 Cairnwork 本機輸出。"
    return f"Cairnwork 修好並通過 Core 驗證，{tail}"


def failure_text(status: dict) -> str:
    word = status["status"] if status["status"] in STATUSES else "UNKNOWN"
    phase = status.get("phase")
    where = f"（最後階段 `{phase}`）" if phase in PHASES else ""
    return f"Cairnwork 沒有完成：`{word}`{where}。細節在 Cairnwork 本機輸出，不貼在這裡。"


def refusal_code(exc: Exception) -> str:
    code = getattr(exc, "code", "")
    return code if isinstance(code, str) and REFUSAL_CODE.fullmatch(code) else "REFUSED"


def require_bot_gh(gh: str) -> None:
    """Fail closed unless --gh is named like the bot's wrapper.

    A misconfiguration guard, not an identity check: any file called nexus-agent-gh passes.
    A real probe (installation token or GraphQL viewer login) is a follow-up Issue."""
    if Path(gh).name != BOT_GH:
        raise RuntimeError(f"GH_IDENTITY_NOT_BOT: --gh must be the {BOT_GH} wrapper")
