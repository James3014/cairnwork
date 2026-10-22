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
test commit elsewhere: the Issue runner never tells it the commit id. A test commit kept in
the private store (deferred_store.py) is on no remote at all; one pushed to a public branch
could still be found by a worker with network access.
"""

from __future__ import annotations

import ast
from pathlib import Path, PurePosixPath

from cairnwork.gitcmd import OID, GitCommandError, run_git
from cairnwork.workspace import ATTEMPT_REF_PREFIX

_IDENTITY = ("-c", "user.name=Cairnwork", "-c", "user.email=cairnwork@localhost")
_REGULAR = ("100644", "100755")
# pytest reads these on its own; a test commit carries tests, not configuration.
# Same names the candidate may not touch (verifiers.protected_verifier_inputs). __init__.py
# stays allowed: it runs only when the tests themselves import their package.
_SITE_NAMES = frozenset({"sitecustomize", "usercustomize"})  # also refused as package names
_NOT_TEST_FILES = frozenset(
    {
        "conftest.py", "pytest.ini", "setup.cfg", "tox.ini", "pyproject.toml",
        "setup.py", "sitecustomize.py", "usercustomize.py",
    }
)  # fmt: skip


class TestCommitRejected(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def never_seen(repo: Path, test_commit: str) -> None:
    """Before the worker starts: the test commit must not be on this machine yet. A retry
    or a sibling task that already fetched it would have left it (and its logs) where the
    worker can read them, so such a task needs a fresh test commit."""
    try:
        run_git(repo, "cat-file", "-e", f"{test_commit}^{{commit}}")
    except GitCommandError:
        return
    raise TestCommitRejected("TEST_COMMIT_ALREADY_EXPOSED", "push the tests as a new commit")


def ensure_test_commit(repo: Path, test_commit: str, store=None) -> None:
    """Fetch the test commit into repo only now, after the worker is done: until then
    nothing on this machine holds the tests (the Issue runner reads its parent via the
    GitHub API, or from the private store). With a store (deferred_store.py, Issue #119)
    the commit comes from its bundle and never from a remote the worker could also reach;
    a ``required`` store refuses a commit that was not imported instead of falling back."""
    if not OID.fullmatch(test_commit):
        raise TestCommitRejected("TEST_COMMIT_INVALID", "not a 40-hex commit id")
    try:
        run_git(repo, "cat-file", "-e", f"{test_commit}^{{commit}}")
        return
    except GitCommandError:
        pass
    if store is not None and store.has(test_commit):
        store.fetch_into(repo, test_commit)
        return
    if store is not None and store.required:
        raise TestCommitRejected("TEST_COMMIT_NOT_IMPORTED", test_commit)
    try:
        run_git(repo, "fetch", "--quiet", "--no-tags", "origin", test_commit)
        run_git(repo, "cat-file", "-e", f"{test_commit}^{{commit}}")
    except GitCommandError as exc:
        raise TestCommitRejected("TEST_COMMIT_UNREADABLE", str(exc)) from exc


def check_roots_on_base(repo: Path, base: str, roots: tuple[str, ...]) -> None:
    """Every part of each configured root that exists on the base must be a real directory:
    a symlink or file there could point the tests (or verifier targets) outside the root."""
    for root in roots:
        parts = root.split("/")
        found_parts = []
        for i, part in enumerate(parts):
            try:  # the parent's entries: a case variant (Tests/) may alias the root on some disks
                where = ["--", "/".join(found_parts) + "/"] if i else []
                raw = run_git(repo, "ls-tree", "-z", base, *where)
            except GitCommandError as exc:
                raise TestCommitRejected("TEST_ROOT_UNREADABLE", str(exc)) from exc
            found = [
                e
                for e in raw.split("\0")
                if e.rsplit("\t", 1)[-1].rsplit("/", 1)[-1].casefold() == part.casefold()
            ]
            if not found:
                break  # absent on the base: nothing to hide behind (a test commit may add it)
            if len(found) > 1 or not found[0].startswith("040000 tree "):
                raise TestCommitRejected(
                    "TEST_ROOT_NOT_A_DIRECTORY", f"{parts[: i + 1]} on the base"
                )
            found_parts.append(found[0].rsplit("\t", 1)[-1].rsplit("/", 1)[-1])


def test_paths(
    repo: Path, base: str, test_commit: str, roots: tuple[str, ...] = ("tests",)
) -> tuple[str, ...]:
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
            or not path.startswith(tuple(f"{r}/" for r in roots))
            or posix.name.casefold() in _NOT_TEST_FILES
            or posix.name.casefold().endswith(".pth")
            or {x.casefold() for x in posix.parts} & _SITE_NAMES
        ):
            raise TestCommitRejected("TEST_COMMIT_SCOPE", f"{status} {new_mode} {path}")
        paths.append(path)
    if not paths:
        raise TestCommitRejected("TEST_COMMIT_EMPTY")
    return tuple(paths)


def new_tests(repo: Path, base: str, test_commit: str, paths: tuple[str, ...]) -> frozenset[str]:
    """Node ids (``path::name`` or ``path::Class::name``) of the test functions the test
    commit adds or changes: only these may count as teeth, not tests in the same file
    that already failed on the base for another reason (e.g. the environment).

    Top-level ``test*`` functions and ``test*`` methods of top-level ``Test*`` classes,
    compared by AST (decorators included, line numbers not). Not seen: tests changed only
    through a fixture, helper or module constant, nested or inherited test classes,
    TestCase subclasses not named Test*, custom python_functions/python_classes. Such a
    test commit is refused (no teeth found), never wrongly accepted."""
    found = set()
    for path in paths:
        if not path.endswith(".py"):
            continue
        before = _test_functions(repo, f"{base}:{path}")
        for node_id, body in _test_functions(repo, f"{test_commit}:{path}").items():
            if before.get(node_id) != body:
                found.add(f"{path}::{node_id}")
    return frozenset(found)


def _test_functions(repo: Path, blob: str) -> dict[str, str]:
    try:
        source = run_git(repo, "show", blob)
    except GitCommandError:
        return {}  # a new file
    except UnicodeDecodeError as exc:
        raise TestCommitRejected("TEST_COMMIT_UNPARSABLE", f"{blob} is not UTF-8") from exc
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        raise TestCommitRejected("TEST_COMMIT_UNPARSABLE", blob) from exc
    functions: dict[str, str] = {}
    defs = (ast.FunctionDef, ast.AsyncFunctionDef)
    for node in tree.body:
        if isinstance(node, defs) and node.name.startswith("test"):
            functions[node.name] = ast.dump(node)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in node.body:
                if isinstance(item, defs) and item.name.startswith("test"):
                    functions[f"{node.name}::{item.name}"] = ast.dump(item)
    return functions


def failed_on_base(runs, targets: frozenset[str]) -> bool:
    """Tests with teeth: one of the new or changed tests failed on the base (the
    verifier's trusted result, report_guard), and no verifier broke in another way
    (exit codes other than 0 and 1)."""
    if any(run.returncode not in (0, 1) for run in runs):
        return False
    for run in runs:
        for nodeid, outcome in run.outcomes.items():
            if outcome == "failed" and nodeid.split("[", 1)[0] in targets:
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
