"""Source lineage (PRESERVED #10): where a task's base and the clone's origin come from.

A base commit must be a commit that is equal to or an ancestor of the tracked ``origin/main``,
and the working clone's ``origin`` must be the repository being served; otherwise the work is
refused before anything is dispatched or pushed.
"""

from __future__ import annotations

import re
from pathlib import Path

from cairnwork.gitcmd import GitCommandError, run_git

ORIGIN = re.compile(r"https://github\.com/([^/:@\s]+/[^/:@\s]+)")
COMMIT = re.compile(r"[0-9a-f]{40}")
MAIN_REFSPEC = "+refs/heads/main:refs/remotes/origin/main"


def verify_origin(clone: Path, repo: str) -> None:
    """Every fetch and push URL of origin must be https://github.com/<repo>.

    ``get-url --push --all origin`` is the set ``git push origin`` uses (insteadOf included),
    so the push must name the remote, never a URL built here. Fails closed: a URL with
    credentials (``user@``), a trailing slash or any other shape is refused, not normalised.
    Callers must run git through ``run_git`` (no GIT_* environment) for the push too, so the
    remote checked here is the remote pushed to."""
    for flags in (("--all",), ("--push", "--all")):
        try:
            urls = run_git(clone, "remote", "get-url", *flags, "origin").splitlines()
        except GitCommandError as exc:
            raise RuntimeError(f"LINEAGE_REMOTE_UNKNOWN: origin has no URL ({exc})") from exc
        if not any(urls):
            raise RuntimeError("LINEAGE_REMOTE_UNKNOWN: origin has no URL")
        for url in urls:
            found = ORIGIN.fullmatch(url)
            if not found:
                raise RuntimeError(f"LINEAGE_REMOTE_UNKNOWN: origin {url!r} is not a github URL")
            if found[1].lower().removesuffix(".git") != repo.lower():
                raise RuntimeError(f"LINEAGE_REPOSITORY_MISMATCH: origin {url!r} is not {repo}")


def require_on_main(clone: Path, base: str, *, fetch: bool = True) -> None:
    """base must be a commit in the history of origin/main (fetched first: a stale tracking
    ref is not trusted, though an ancestor of an older main is still an ancestor of the new)."""
    if not COMMIT.fullmatch(base):
        raise RuntimeError(f"LINEAGE_BASE_NOT_COMMIT: {base!r} is not a 40-hex commit id")
    if fetch:
        try:
            run_git(clone, "fetch", "--quiet", "origin", MAIN_REFSPEC)
        except GitCommandError as exc:
            raise RuntimeError(
                f"LINEAGE_FETCH_FAILED: could not fetch origin main ({exc})"
            ) from exc
    try:
        if run_git(clone, "cat-file", "-t", base).strip() != "commit":
            raise RuntimeError(f"LINEAGE_BASE_NOT_COMMIT: {base!r} is not a commit")
    except GitCommandError as exc:
        raise RuntimeError(f"LINEAGE_BASE_NOT_COMMIT: {base!r} is not a commit here") from exc
    try:
        run_git(clone, "merge-base", "--is-ancestor", base, "refs/remotes/origin/main")
    except GitCommandError as exc:
        raise RuntimeError(f"LINEAGE_BASE_NOT_ON_MAIN: {base} is not in origin/main") from exc
