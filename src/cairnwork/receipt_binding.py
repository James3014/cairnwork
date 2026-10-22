"""PRESERVED #15: a stored Core request is bound to a change set read from git (Issue #61)."""

from __future__ import annotations

from typing import Any

from cairnwork.changeset import GitChangeSet
from cairnwork.core_request import change_manifest
from cairnwork_protocol import change_manifest_hash


def request_matches(request: dict[str, Any], change: GitChangeSet) -> bool:
    """Whether ``request`` was built for exactly ``change``; nothing a worker says is trusted."""
    manifest, stored = change_manifest(change), request.get("change_set")
    return isinstance(stored, dict) and (
        stored.get("diff_hash") == change_manifest_hash(manifest)
        and request.get("change_manifest") == manifest
        and stored.get("paths") == change.paths
        and stored.get("deleted_paths") == change.deleted_paths
        and stored.get("source_revision") == f"git-commit:{change.base_commit}"
        and stored.get("target_revision") == f"git-commit:{change.candidate_commit}"
    )
