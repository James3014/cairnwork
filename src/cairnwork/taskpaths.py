"""PRESERVED #4: ``allowed_files`` and verifier targets are checked against the base tree
before dispatch, so a task that can only fail (a symlinked file, a missing directory, a
missing test) is refused now and not after the worker's whole time limit. The snapshot
and candidate checks stay as the second line."""

from __future__ import annotations

from pathlib import Path

from cairnwork.gitcmd import GitCommandError, run_git
from cairnwork.pipeline import TaskRejected, TaskSpec
from cairnwork.verifier_spec import VerifierSpec

_REGULAR = ("100644", "100755")


def check_task_paths(
    repo: Path,
    base_commit: str,
    allowed_files: tuple[str, ...],
    verifier_targets: tuple[str, ...],
    test_paths: tuple[str, ...] = (),
) -> None:
    """Every parent of an allowed file is a real directory on the base, the file itself is
    a regular file or absent, and a verifier target is on the base, an allowed file or in
    ``test_paths`` (what the test commit adds)."""
    targets = {t.split("::", 1)[0]: t for t in verifier_targets}
    wanted = {p for path in (*allowed_files, *targets) for p in _prefixes(path)}
    try:
        raw = run_git(repo, "ls-tree", "-r", "-t", "-z", base_commit, "--", *sorted(wanted))
    except GitCommandError as exc:
        raise TaskRejected("TASK_PATH_UNREADABLE", str(exc)) from exc
    modes = {e.split("\t", 1)[1]: e.split()[0] for e in raw.split("\0") if e}
    for path in allowed_files:
        for parent in _prefixes(path)[:-1]:
            if modes.get(parent) != "040000":
                raise TaskRejected("TASK_PATH_PARENT_INVALID", f"{path}: {parent} on the base")
        if modes.get(path, "100644") not in _REGULAR:
            raise TaskRejected("TASK_PATH_NOT_REGULAR_FILE", f"{path} is {modes[path]} on the base")
    for path in targets:
        if path not in modes and path not in (*allowed_files, *test_paths):
            raise TaskRejected("TASK_VERIFIER_TARGET_MISSING", path)
        if modes.get(path, "100644") not in _REGULAR:
            raise TaskRejected("TASK_PATH_NOT_REGULAR_FILE", f"{path} is {modes[path]} on the base")


def check_submitted_paths(task: TaskSpec, specs: tuple[VerifierSpec, ...]) -> None:
    """The check for a request. A test commit is not fetched before the worker is done
    (deferred_tests.py), so a task with one cannot have its targets compared with it."""
    targets = () if task.test_commit else tuple(t for spec in specs for t in spec.targets)
    check_task_paths(task.repo, task.base_commit, task.allowed_files, targets)


def _prefixes(path: str) -> list[str]:
    parts = path.split("/")
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]
