"""Visible-test feedback (experimental, default off; Issue #32): after a worker attempt,
Cairnwork runs the task's visible verification commands in the verifier sandbox and, if they
fail, gives the worker up to ``retries`` more turns (no default: an explicit opt-in) with the
pass/fail summary. Not wired into any runner;
see feedback.py."""

from experimental.visible_tests.feedback import VisibleTestsWorker, visible_specs

__all__ = ["VisibleTestsWorker", "visible_specs"]
