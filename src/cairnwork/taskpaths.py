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
_NEW_DIR_HINT = (
    "new directories are refused: create the directory in the base commit first, "
    "or list a path under an existing directory"
)


def check_task_paths(
    repo: Path,
    base_commit: str,
    allowed_files: tuple[str, ...],
    verifier_targets: tuple[str, ...],
) -> None:
    """Every parent of an allowed file is a real directory on the base (a task may not
    create a directory: fail closed, #149), the file itself is a regular file or absent,
    and a verifier target is on the base or an allowed file."""
    targets = {t.split("::", 1)[0]: t for t in verifier_targets}
    wanted = {p for path in (*allowed_files, *targets) for p in _prefixes(path)}
    try:
        raw = run_git(repo, "ls-tree", "-r", "-t", "-z", base_commit, "--", *sorted(wanted))
    except GitCommandError as exc:
        raise TaskRejected("TASK_PATH_UNREADABLE", str(exc)) from exc
    modes = {e.split("\t", 1)[1]: e.split()[0] for e in raw.split("\0") if e}
    for path in allowed_files:
        for parent in _prefixes(path)[:-1]:
            mode = modes.get(parent)
            if mode != "040000":
                what = "does not exist" if mode is None else f"is {mode}, not a directory,"
                detail = f"{path}: {parent} {what} on the base; {_NEW_DIR_HINT}"
                raise TaskRejected("TASK_PATH_PARENT_INVALID", detail)
        if modes.get(path, "100644") not in _REGULAR:
            raise TaskRejected("TASK_PATH_NOT_REGULAR_FILE", f"{path} is {modes[path]} on the base")
    for path in targets:
        if path not in modes and path not in allowed_files:
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
