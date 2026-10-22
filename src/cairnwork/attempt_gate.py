"""The per-attempt trust re-check for an Issue task (PRESERVED #2): before every attempt the
Issue's task block and its queueing must still be what was trusted at dispatch."""

from __future__ import annotations

from collections.abc import Callable

from cairnwork.issue_runner import QUEUE, TRUST_CHANGED, GitHub
from cairnwork.issue_task import parse_task_block


def attempt_gate(
    github: GitHub, number: int, block_sha256: str, roots: tuple[str, ...]
) -> Callable[[], str]:
    """The refusal code (or "") for the Issue; anything unreadable is a refusal."""

    def gate() -> str:
        try:
            body = github.current(number).get("body") or ""
            if parse_task_block(body, roots).block_sha256 != block_sha256:
                return TRUST_CHANGED
            return TRUST_CHANGED if github.trust(number, QUEUE) else ""
        except Exception:  # noqa: BLE001 - fail closed
            return TRUST_CHANGED

    return gate
