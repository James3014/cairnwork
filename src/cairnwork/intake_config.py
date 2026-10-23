"""The Intake's state-directory config: its defaults and the checks that need no state."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cairnwork.p2p_selection import DEFAULT_BUDGET_SECONDS, DEFAULT_MODE

# verifier_python: the target repository's own interpreter (scripts/setup_target_venv.sh),
# an absolute path; None runs verifiers with the account's (or Cairnwork's) Python.
# One per state directory, so one state directory serves one target repository.
DEFAULT_CONFIG = {
    "model": "gemini-3.8-flash-high",
    "agy": "agy",
    # An account-pool dispatcher run instead of agy (workers/agy_dispatch.py): an absolute
    # path, and its timing in seconds; None runs agy itself.
    "agy_dispatch": None,
    "agy_dispatch_pool_wait_seconds": 600,
    "agy_dispatch_provider_stall_seconds": 900,
    "agy_dispatch_stream_no_progress_seconds": 900,
    # The account the dispatcher runs agy as (--run-as-user, #183); None: the owner, as before.
    "agy_run_as_user": None,
    "verifier_account": True,
    "verifier_python": None,
    # The gh the Issue runner was started with (cairnwork-issues writes it), so the runner
    # started by submit re-checks trust with the same one; None means DEFAULT_GH.
    "gh": None,
    # Cairnwork's own repository for this target: the base is fetched into it from the
    # request's repo (read-only), and every ref or object Cairnwork writes lands here,
    # never in the caller's repository (e.g. the Gateway's live Controller).
    "mirror": None,
    # Nexus Core as an HTTP service (cairnwork_protocol.http) instead of in-process:
    # its root URL and a file holding its bearer token (never the token itself).
    "core_url": None,
    # The identity (Core's core_identity object) a remote Core must report; None pins the
    # Core installed beside Cairnwork.
    "core_identity": None,
    "core_token_file": None,
    # Where verifier targets and deferred tests may live (issue_task.check_test_roots).
    "test_roots": ["tests"],
    # Dirs prepended to the verifier's import path so a src-layout project can import its own
    # package (#82, issue_task.check_import_paths); default empty keeps the behaviour unchanged.
    "import_paths": [],
    "test_source": "auto",
    # A new task's base must be in origin/main's history (lineage.require_on_main, the #76
    # class) before it is bound; False only for a repository that has no origin. This
    # submit-time check guards against mistakes, not a hostile caller: verify_origin is not
    # run on the route path.
    "require_base_on_main": True,
    # P2P widening (#117, p2p.py): whole-suite | module-dir | off (opt-out); seconds per run.
    "p2p_selection": DEFAULT_MODE,
    "p2p_time_budget_seconds": DEFAULT_BUDGET_SECONDS,
    # An opt-in executable the Issue runner runs after a PR is opened for a Core-verified task
    # (post_verified.py): an absolute path, and its time limit in seconds. Its failure is
    # recorded and alerted, never a verdict; None (the default) runs nothing.
    "post_verified_hook": None,
    "post_verified_hook_seconds": 300,
}


def seconds_ok(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 86400


def has_token(token_file: Any) -> bool:
    """A regular file holding a token; it is read again at each Core call (rotation)."""
    try:
        path = Path(token_file)
        return path.is_file() and bool(path.read_text(encoding="utf-8").strip())
    except (OSError, TypeError, ValueError):
        return False
