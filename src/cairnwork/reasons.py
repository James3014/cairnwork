"""The reason text of a judged attempt, and why a verifier run did not pass (Issue #114).

A run passes only if every check holds (verifiers._run_one); Core then sees a bare FAIL and
answers FAILED_VERIFICATION, which says nothing about a run whose log shows passing tests.
The reasons are recorded next to the verifier id, so a failure is always explainable.
"""

from __future__ import annotations

import re

MAX_NAMES = 5
MAX_NAME_CHARS = 120
# Control characters, the record delimiters of explain() ("[", "]", ";", ":") and bidi controls.
_CONTROL = re.compile(
    r"[\x00-\x1f\x7f-\x9f\u2028\u2029\u200e\u200f\u202a-\u202e\u2066-\u2069\[\];:]"
)


def _names(names) -> str:
    """A count and at most MAX_NAMES names, each cut and stripped of control characters: the
    names are written by the code under test and end up in a recorded reason."""
    items = list(names)
    shown = [_CONTROL.sub("?", str(n))[:MAX_NAME_CHARS] for n in items[:MAX_NAMES]]
    more = len(items) - len(shown)
    return f"{len(items)}: {', '.join(shown)}" + (f", +{more} more" if more > 0 else "")


def verdict_reason(verified: bool, verdict, recorded) -> str:
    """The reason string for a judged attempt. A verified attempt reads ``CORE_<status>``. Otherwise
    the readback's reason is shown (``recorded`` when present, so an unverified readback never reads
    as CORE_VERIFIED; ``verdict`` only if there is no readback); when that reason is empty, a Core
    that said VERIFIED but did not verify on readback reads ``CORE_NOT_VERIFIED``, else
    ``CORE_<status>``."""
    if verified:
        return f"CORE_{verdict.status}"
    shown = recorded or verdict
    if shown.reason:
        return shown.reason
    return "CORE_NOT_VERIFIED" if verdict.status == "VERIFIED" else f"CORE_{verdict.status}"


def why_not_passed(
    *, ran_sandboxed: bool, returncode, timed_out: bool, mutated, survivors: int,
    log_too_big: bool, tests_ran: bool,
) -> str:  # fmt: skip
    """The causes that made a run fail, ``""`` if none (then the run passed)."""
    causes = []
    if not ran_sandboxed:
        causes.append("no sandbox backend: verifier not executed")
    if timed_out:
        causes.append("timed out")
    if mutated:
        causes.append(f"checkout changed ({_names(mutated)})")
    if survivors:
        causes.append(
            f"{survivors} process(es) outlived the run, or the process list was unreadable"
        )
    if log_too_big:
        causes.append("log over the size cap")
    if ran_sandboxed and not timed_out and returncode != 0:
        causes.append(f"pytest exit code {returncode}")
    elif ran_sandboxed and returncode == 0 and not tests_ran:
        causes.append("pytest passed but the guard saw no test run")
    return "; ".join(causes)


def explain(runs) -> str:
    """``[v1: cause; v2: cause]`` for the runs that failed; ``""`` when every run passed."""
    failed = [
        f"{run.spec.verifier_id}: {run.why or 'no cause recorded'}"
        for run in runs
        if not run.observation.passed
    ]
    return f" [{'; '.join(failed)}]" if failed else ""
