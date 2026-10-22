"""Pick the existing (pass-to-pass) tests to run alongside a task's own verifiers (#117).

A task's author declares the fail-to-pass verifiers that must pass on the candidate. Those
catch "did the change do the job", not "did the change quietly break something that already
worked". Widening runs EXISTING tests (ones that pass on the base) over the candidate too.

This module is pure selection: given the files a candidate may change, the test files that
exist on the base and a mode, it returns the test files to consider. It runs nothing and
knows nothing about pass or fail; p2p.py finds which of them pass on the base.

Modes (state config ``p2p_selection``):

* ``off`` - no widening; only the author's verifiers run.
* ``whole-suite`` (default) - every existing test file under the test roots.
* ``module-dir`` - the tests in the directories that hold a test for a changed file. A changed
  file maps to a test by mirror (strip a leading ``src`` and the top-level package, look in
  ``<test_root>/<remaining dirs>``) or by name (``test_<stem>.py`` / ``<stem>_test.py``
  anywhere under the roots). Every test directly in a matched test's directory is selected:
  on a flat layout ``tests/test_base.py`` brings in its sibling ``tests/test_sibling.py``
  (the regression #117 is about). A changed file that maps to nothing selects the tests
  directly in the test roots, never the whole tree. Changed test files are not sources.
"""

from __future__ import annotations

from posixpath import basename, dirname, splitext

MODES = ("off", "whole-suite", "module-dir")
DEFAULT_MODE = "whole-suite"  # Intake (cairnwork-route, issue runner)
DIRECT_MODE = "off"  # TaskSpec built directly (pipeline.run_task, drills): no P2P unless set
DEFAULT_BUDGET_SECONDS = 300  # seconds per P2P run (probe, candidate run, re-run)


def _under_root(path: str, roots: tuple[str, ...]) -> bool:
    return any(path == r or path.startswith(f"{r}/") for r in roots)


def _mirror_subpath(changed_dir: str) -> str:
    """Drop a leading ``src`` and then the top-level package; what remains mirrors under a
    test root (possibly empty)."""
    parts = [p for p in changed_dir.split("/") if p]
    if parts and parts[0] == "src":
        parts = parts[1:]
    return "/".join(parts[1:])


def select_p2p_targets(
    changed_files: tuple[str, ...],
    existing_tests: tuple[str, ...],
    test_roots: tuple[str, ...],
    mode: str = DEFAULT_MODE,
) -> tuple[str, ...]:
    """Existing test files to consider as P2P candidates (sorted, de-duplicated). Paths are
    repo-relative POSIX; ``existing_tests`` are the test files on the base under ``test_roots``.
    Raises ValueError on an unknown mode."""
    if mode not in MODES:
        raise ValueError(f"unknown p2p_selection mode {mode!r}; one of {MODES}")
    if mode == "off" or not existing_tests:
        return ()
    tests = frozenset(existing_tests)
    if mode == "whole-suite":
        return tuple(sorted(tests))
    root_level = {dirname(t) for t in tests if dirname(t) in test_roots}
    dirs: set[str] = set()
    for changed in changed_files:
        if _under_root(changed, test_roots):
            continue
        tail = _mirror_subpath(dirname(changed))
        mirror = {f"{r}/{tail}" for r in test_roots} if tail else set()
        stem = splitext(basename(changed))[0]
        names = {f"test_{stem}.py", f"{stem}_test.py"}
        hit = {dirname(t) for t in tests if dirname(t) in mirror or basename(t) in names}
        dirs |= hit or root_level
    return tuple(sorted(t for t in tests if dirname(t) in dirs))


def check_p2p_config(loaded: dict) -> None:
    """The state config's ``p2p_selection`` and ``p2p_time_budget_seconds``; ValueError if bad."""
    if loaded.get("p2p_selection", DEFAULT_MODE) not in MODES:
        raise ValueError(f"p2p_selection must be one of {sorted(MODES)}")
    seconds = loaded.get("p2p_time_budget_seconds", 1)
    if not isinstance(seconds, int) or isinstance(seconds, bool) or not 1 <= seconds <= 86400:
        raise ValueError("p2p_time_budget_seconds must be a whole number of seconds")
