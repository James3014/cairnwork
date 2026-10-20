"""Deferred tests: the tests that prove a fix are applied only after the worker is done.

A task may name a ``test_commit``: a commit whose only parent is the task's base and
which adds or changes files under ``tests/`` only (no ``conftest.py``, regular files
only). The worker never gets it: its attempt clone holds the base. Cairnwork then

1. runs the verifiers on the test commit itself (base + tests) and requires a real
   failure there (pytest exit 1 for at least one verifier, and no verifier erroring
   with any other non-zero code): tests that already pass, or only fail to import,
   prove nothing about the fix;
2. lays the test files over the worker's candidate in a private index and commits the
   result on top of it; that commit is what the verifiers run, Core notarises and a PR
   would carry (fix and tests together).

So the worker can neither read nor weaken the tests that judge it (the same idea as
SWE-bench applying its tests after the model's patch), as long as it cannot reach the
test commit elsewhere: the Issue runner never tells it the commit id, but a worker
with network access could still look for a public test branch.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from xml.etree import ElementTree  # noqa: S405 - JUnit from our own verifier run, 4 MB cap

from cairnwork.gitcmd import OID, GitCommandError, run_git
from cairnwork.workspace import ATTEMPT_REF_PREFIX

_IDENTITY = ("-c", "user.name=Cairnwork", "-c", "user.email=cairnwork@localhost")
_REGULAR = ("100644", "100755")
# pytest reads these on its own; a test commit carries tests, not configuration.
_NOT_TEST_FILES = frozenset({"conftest.py", "pytest.ini", "setup.cfg", "tox.ini", "pyproject.toml"})


class TestCommitRejected(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def ensure_test_commit(repo: Path, test_commit: str) -> None:
    """Fetch the test commit into repo only now, after the worker is done: until then
    nothing on this machine holds the tests (the Issue runner reads its parent via the
    GitHub API)."""
    if not OID.fullmatch(test_commit):
        raise TestCommitRejected("TEST_COMMIT_INVALID", "not a 40-hex commit id")
    try:
        run_git(repo, "cat-file", "-e", f"{test_commit}^{{commit}}")
        return
    except GitCommandError:
        pass
    try:
        run_git(repo, "fetch", "--quiet", "--no-tags", "origin", test_commit)
        run_git(repo, "cat-file", "-e", f"{test_commit}^{{commit}}")
    except GitCommandError as exc:
        raise TestCommitRejected("TEST_COMMIT_UNREADABLE", str(exc)) from exc


def test_paths(repo: Path, base: str, test_commit: str) -> tuple[str, ...]:
    """The files the test commit adds or changes; refuse anything but plain test files."""
    try:
        parents = run_git(repo, "rev-list", "--parents", "-n", "1", test_commit).split()
        raw = run_git(
            repo, "diff-tree", "-r", "-z", "--no-renames", "--no-commit-id", "--raw",
            base, test_commit,
        )  # fmt: skip
    except GitCommandError as exc:
        raise TestCommitRejected("TEST_COMMIT_UNREADABLE", str(exc)) from exc
    if parents[1:] != [base]:
        raise TestCommitRejected("TEST_COMMIT_NOT_ON_BASE", "its only parent must be the base")
    fields = raw.split("\0")
    paths = []
    for meta, path in zip(fields[0::2], fields[1::2], strict=False):
        new_mode, status = meta.split()[1], meta.split()[4]
        posix = PurePosixPath(path)
        if (
            status not in ("A", "M")
            or new_mode not in _REGULAR
            or posix.parts[0] != "tests"
            or posix.name.casefold() in _NOT_TEST_FILES
        ):
            raise TestCommitRejected("TEST_COMMIT_SCOPE", f"{status} {new_mode} {path}")
        paths.append(path)
    if not paths:
        raise TestCommitRejected("TEST_COMMIT_EMPTY")
    return tuple(paths)


def failed_on_base(runs, paths: tuple[str, ...]) -> bool:
    """Tests with teeth: a test case from the deferred test files failed (JUnit), and
    no verifier broke in another way (exit codes other than 0 and 1)."""
    if any(run.returncode not in (0, 1) for run in runs):
        return False
    modules = {p[:-3].replace("/", ".") for p in paths if p.endswith(".py")}
    for run in runs:
        report = Path(run.log_path).parent / "run" / "junit.xml"
        try:
            cases = ElementTree.parse(report).getroot().iter("testcase")
        except (OSError, ElementTree.ParseError):
            continue
        for case in cases:
            owner = case.get("classname") or ""
            failed = case.find("failure") is not None
            if failed and any(owner == m or owner.startswith(m + ".") for m in modules):
                return True
    return False


def lay_tests_over(
    repo: Path, candidate: str, test_commit: str, paths: tuple[str, ...], attempt_id: str,
    run_root: Path,
) -> str:  # fmt: skip
    """Commit the test files of test_commit on top of candidate; return the new commit."""
    index = Path(run_root) / f"{attempt_id}.tests.index"
    if index.exists() or index.is_symlink():
        raise TestCommitRejected("TEST_OVERLAY_FAILED", f"{index} already exists")
    try:
        run_git(repo, "read-tree", candidate, index_file=index)
        for path in paths:
            mode, _kind, blob = run_git(repo, "ls-tree", test_commit, "--", path).split()[:3]
            run_git(
                repo, "update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}",
                index_file=index,
            )  # fmt: skip
        tree = run_git(repo, "write-tree", index_file=index).strip()
        message = f"Cairnwork attempt {attempt_id}: tests from {test_commit}"
        head = run_git(repo, *_IDENTITY, "commit-tree", tree, "-p", candidate, "-m", message)
        head = head.strip()
        run_git(repo, "update-ref", f"{ATTEMPT_REF_PREFIX}{attempt_id}-tested", head)
    except (GitCommandError, ValueError) as exc:
        raise TestCommitRejected("TEST_OVERLAY_FAILED", str(exc)) from exc
    finally:
        index.unlink(missing_ok=True)
    return head
