"""Parse a task's untrusted verifier argv and name the inputs a candidate must not touch.

Verifiers come from the task, so they are untrusted text. Only one shape is
accepted: ``python3 -m pytest [-q] [-p no:cacheprovider] <test root>/<file>.py[::<name>]...``.
Anything else is rejected before it runs, and nothing goes through a shell. The
run itself lives in verifiers.py; this module is only the gate on what may run and
on which files a candidate is allowed to change, kept separate so the parsing
rules stay small and easy to audit.
"""

from __future__ import annotations

from dataclasses import dataclass

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


def protected_verifier_inputs(paths: tuple[str, ...], roots=DEFAULT_TEST_ROOTS) -> tuple[str, ...]:
    """Paths a candidate must not change, because they decide what the verifier runs."""
    return tuple(
        path
        for path in paths
        if path in roots
        or path.startswith(tuple(f"{r}/" for r in roots))
        or path.rsplit("/", 1)[-1] in _PROTECTED_NAMES
        or ("/" not in path and path.endswith((".py", ".pth")))
    )
