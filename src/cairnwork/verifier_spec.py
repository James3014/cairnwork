"""Parse a task's untrusted verifier argv and name the inputs a candidate must not touch.

Verifiers come from the task, so they are untrusted text. Only one shape is
accepted: ``python3 -m pytest [-q] [-p no:cacheprovider] <test root>/<file>.py[::<name>]...``.
Anything else is rejected before it runs, and nothing goes through a shell. The
run itself lives in verifiers.py; this module is only the gate on what may run and
on which files a candidate is allowed to change, kept separate so the parsing
rules stay small and easy to audit.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from cairnwork.gitcmd import GitCommandError, run_git
from cairnwork.issue_task import DEFAULT_TEST_ROOTS, test_target

_OPTIONS = {("-q",), ("-p", "no:cacheprovider")}
_PROTECTED_NAMES = frozenset(
    {
        "conftest.py",
        "pytest.ini",
        "pyproject.toml",
        "setup.cfg",
        "tox.ini",
        "setup.py",
        "sitecustomize.py",
        "usercustomize.py",
    }
)
_MODULE_NAMES = (
    "import json, pkgutil, sys; from importlib.metadata import packages_distributions; "
    "print(json.dumps(sorted(sys.stdlib_module_names | "
    "{module.name for module in pkgutil.iter_modules()} | "
    "set(packages_distributions()))))"
)


class VerifierRejected(Exception):
    code = "VERIFIER_NOT_ALLOWED"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


@dataclass(frozen=True)
class VerifierSpec:
    verifier_id: str
    targets: tuple[str, ...]


def parse_verifier(verifier_id: str, argv: list[str], roots=DEFAULT_TEST_ROOTS) -> VerifierSpec:
    """Accept only python3 -m pytest with allowed options and <test root>/*.py targets."""
    if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
        raise VerifierRejected("argv must be a list of strings")
    if argv[:3] != ["python3", "-m", "pytest"]:
        raise VerifierRejected(f"{verifier_id}: only 'python3 -m pytest' is allowed")
    rest, targets, target = argv[3:], [], test_target(roots)
    while rest:
        if tuple(rest[:1]) in _OPTIONS:
            rest = rest[1:]
        elif tuple(rest[:2]) in _OPTIONS:
            rest = rest[2:]
        elif target.fullmatch(rest[0]):
            targets.append(rest.pop(0))
        else:
            raise VerifierRejected(f"{verifier_id}: argument {rest[0]!r} is not allowed")
    if not targets:
        raise VerifierRejected(f"{verifier_id}: no {'|'.join(roots)}/*.py target")
    return VerifierSpec(verifier_id, tuple(targets))


def protected_verifier_inputs(
    paths: tuple[str, ...],
    roots=DEFAULT_TEST_ROOTS,
    *,
    repo: Path | None = None,
    base_commit: str | None = None,
    python: str | None = None,
) -> tuple[str, ...]:
    """Paths a candidate must not change, because they decide what the verifier runs.

    A root module is permitted only when the base tracks a regular file and its
    name cannot shadow a module visible to the verifier's interpreter. Without
    that evidence, root modules remain protected.
    """
    root_modules = tuple(path for path in paths if "/" not in path and path.endswith(".py"))
    safe_roots = _safe_root_modules(root_modules, repo, base_commit, python)
    return tuple(
        path
        for path in paths
        if path in roots
        or path.startswith(tuple(f"{r}/" for r in roots))
        or path.rsplit("/", 1)[-1] in _PROTECTED_NAMES
        or ("/" not in path and path.endswith(".pth"))
        or (path in root_modules and path not in safe_roots)
    )


def _safe_root_modules(
    paths: tuple[str, ...], repo: Path | None, base_commit: str | None, python: str | None
) -> set[str]:
    if not paths or repo is None or base_commit is None:
        return set()
    try:
        listing = run_git(repo, "ls-tree", "-z", base_commit, "--", *paths)
        names = _importable_names(python or sys.executable)
    except (GitCommandError, OSError, ValueError, subprocess.SubprocessError):
        return set()  # no proof of safety: refuse before binding or verification
    tracked = {}
    for entry in filter(None, listing.split("\0")):
        meta, path = entry.split("\t", 1)
        mode, kind, _oid = meta.split()
        tracked[path] = kind == "blob" and mode in ("100644", "100755")
    return {
        path
        for path in paths
        if tracked.get(path) and path[:-3].isidentifier() and path[:-3].casefold() not in names
    }


def _importable_names(python: str) -> set[str]:
    result = subprocess.run(  # noqa: S603 - the configured verifier Python, fixed code
        [python, "-I", "-c", _MODULE_NAMES],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
        cwd="/",
    )
    names = json.loads(result.stdout)
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise ValueError("verifier module inventory is invalid")
    return {name.casefold() for name in names} | {"_cairnwork_report_guard"}
