"""The intake's closed request schema: a submitted payload becomes a TaskSpec or is refused
(``IntakeRefused``); unknown or missing fields are refused, never ignored. intake.py (the state
directory, config and task lifecycle) uses it and re-exports both names."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from cairnwork.pipeline import TaskSpec

FIELDS = frozenset(
    {
        "idempotency_key",
        "card_hash",
        "title",
        "instructions",
        "repo",
        "base_commit",
        "allowed_files",
        "verifiers",
    }
)


class IntakeRefused(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def task_from_payload(
    payload: Any, test_roots: tuple[str, ...] = ("tests",), test_store=None
) -> TaskSpec:
    """A closed schema: unknown or missing fields are refused, never ignored."""
    if not isinstance(payload, dict) or not FIELDS <= set(payload) <= FIELDS | {"test_commit"}:
        got = sorted(payload) if isinstance(payload, dict) else type(payload).__name__
        raise IntakeRefused(
            "BAD_REQUEST", f"fields must be {sorted(FIELDS)} (+test_commit), got {got}"
        )
    if not isinstance(payload.get("test_commit", ""), str):
        raise IntakeRefused("BAD_REQUEST", "test_commit must be a string")
    texts = ("idempotency_key", "card_hash", "title", "instructions", "repo", "base_commit")
    if not all(isinstance(payload[name], str) for name in texts):
        raise IntakeRefused("BAD_REQUEST", "text fields must be strings")
    files, verifiers = payload["allowed_files"], payload["verifiers"]
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        raise IntakeRefused("BAD_REQUEST", "allowed_files must be a list of strings")
    if not isinstance(verifiers, list) or not all(_verifier_shape(v) for v in verifiers):
        raise IntakeRefused("BAD_REQUEST", "verifiers must be [[id, [argv...]], ...]")
    if not os.path.isabs(payload["repo"]):
        raise IntakeRefused("BAD_REQUEST", "repo must be an absolute path")
    return TaskSpec(
        idempotency_key=payload["idempotency_key"],
        card_hash=payload["card_hash"],
        title=payload["title"],
        instructions=payload["instructions"],
        repo=Path(payload["repo"]),
        base_commit=payload["base_commit"],
        allowed_files=tuple(files),
        verifiers=tuple((v[0], tuple(v[1])) for v in verifiers),
        test_commit=payload.get("test_commit", ""),
        test_roots=test_roots,
        test_store=test_store,
    )


def _verifier_shape(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], list)
        and all(isinstance(a, str) for a in value[1])
    )
