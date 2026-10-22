"""What cairnwork-issues may say on GitHub (PRESERVED #24).

Comments carry only allow-listed words: a fixed status, a phase from the journal's fixed list
and an error *code*. Never ``detail`` (a host path after a crash), exception text, worker output
or Issue text. Those stay in the local JSON output."""

from __future__ import annotations

from pathlib import Path

from cairnwork.journal import PHASES

STATUSES = frozenset({"FAILED", "CANCELLED", "SUPERSEDED", "COMPLETED"})
BOT_GH = "nexus-agent-gh"


def failure_text(status: dict) -> str:
    word = status["status"] if status["status"] in STATUSES else "UNKNOWN"
    phase = status.get("phase")
    where = f"（最後階段 `{phase}`）" if phase in PHASES else ""
    return f"Cairnwork 沒有完成：`{word}`{where}。細節在 Cairnwork 本機輸出，不貼在這裡。"


def refusal_code(exc: Exception) -> str:
    code = getattr(exc, "code", "")
    return code if isinstance(code, str) and code.replace("_", "").isalnum() else "REFUSED"


def require_bot_gh(gh: str) -> None:
    """Fail closed unless --gh is the bot's wrapper (a personal ``gh`` would write as a person)."""
    if Path(gh).name != BOT_GH:
        raise RuntimeError(f"GH_IDENTITY_NOT_BOT: --gh must be the {BOT_GH} wrapper")
