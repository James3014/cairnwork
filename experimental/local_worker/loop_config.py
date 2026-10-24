"""The frozen settings of a local worker run (#88): its limits, its task and the model settings.
agent.py re-exports them (``from experimental.local_worker.agent import Limits, Task``)."""

from __future__ import annotations

from dataclasses import dataclass

from experimental.local_worker.tools import KILL_GRACE_SECONDS
from experimental.local_worker.write_scope import normalize


@dataclass(frozen=True)
class Limits:
    context_cap: int
    max_steps: int = 40
    max_wall_seconds: float = 1800.0
    cut_seconds: float = 900.0
    tool_cap_seconds: float = 300.0
    cleanup_reserve_seconds: float = 60.0
    max_completion_tokens: int = 4096

    def __post_init__(self) -> None:
        if self.step_seconds < 1.0:
            raise ValueError(f"cleanup_reserve_seconds must be >= {3 * (KILL_GRACE_SECONDS + 1)}")

    @property
    def step_seconds(self) -> float:
        """Timeout of each bookkeeping step (status, sweep, numstat): a third of the reserve,
        less the kill grace a timed-out step may still use."""
        return self.cleanup_reserve_seconds / 3 - KILL_GRACE_SECONDS


@dataclass(frozen=True)
class Task:
    task_id: str
    repo: str
    prompt: str
    public_tests: tuple[str, ...]


@dataclass(frozen=True)
class Settings:
    model: str
    limits: Limits
    seed: int | None = None
    temperature: float = 0.0
    # write_file/edit_file only under these repo-relative entries (write_scope.py); None: anywhere
    write_scope: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "write_scope", normalize(self.write_scope))
