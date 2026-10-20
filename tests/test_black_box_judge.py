"""The black-box judge gives a forge-proof verdict: the candidate never sees the expected answers,
so it can only pass by being correct. These tests reproduce the forgery attacks report_guard cannot
stop in-process and show the external observer rejects them."""

import sys

import pytest

from experimental.black_box_judge import (
    BlackBoxCase,
    BlackBoxError,
    run_black_box_judge,
)

PY = sys.executable


def checkout_with(tmp_path, source: str):
    path = tmp_path / "checkout"
    path.mkdir()
    (path / "candidate.py").write_text(source)
    return path


def judge(tmp_path, source, cases, **kw):
    return run_black_box_judge(
        checkout_with(tmp_path, source), "candidate:solve", cases, python=PY, **kw
    )


# --- input validation: no sandbox needed, fails closed -------------------------------------


@pytest.mark.parametrize("target", ["nope", "mod:", ":fn", "mod fn", "mod:fn()"])
def test_bad_target_is_rejected(tmp_path, target):
    path = tmp_path / "checkout"
    path.mkdir()
    with pytest.raises(BlackBoxError):
        run_black_box_judge(path, target, (BlackBoxCase((1,), 1),), python=PY)


def test_relative_python_is_rejected(tmp_path):
    path = tmp_path / "checkout"
    path.mkdir()
    with pytest.raises(BlackBoxError):
        run_black_box_judge(path, "candidate:solve", (BlackBoxCase((1,), 1),), python="python3")


def test_no_cases_is_rejected(tmp_path):
    path = tmp_path / "checkout"
    path.mkdir()
    with pytest.raises(BlackBoxError):
        run_black_box_judge(path, "candidate:solve", (), python=PY)


# --- execution: needs real Seatbelt --------------------------------------------------------

pytestmark = pytest.mark.sandbox

CASES = (BlackBoxCase((1, 1), 2), BlackBoxCase((2, 3), 5), BlackBoxCase((10, 20), 30))


def test_honest_candidate_is_verified(tmp_path):
    verdict = judge(tmp_path, "def solve(a, b):\n    return a + b\n", CASES)
    assert verdict.verified
    assert verdict.executed
    assert verdict.n_correct == verdict.n_total == 3


def test_forge_by_early_exit_is_rejected(tmp_path):
    # The classic in-process forgery: print a pass banner and os._exit(0) before doing the work.
    source = (
        "import os, sys\n"
        "print('=== ALL TESTS PASSED ===')\n"
        "sys.stdout.flush()\n"
        "os._exit(0)\n"
        "def solve(a, b):\n    return a + b\n"
    )
    verdict = judge(tmp_path, source, CASES)
    assert not verdict.verified
    assert not verdict.executed  # import died before any case ran
    assert verdict.n_correct == 0


def test_forge_by_wrong_constant_is_rejected(tmp_path):
    # It can return anything it likes; without the expected answers it cannot match them.
    verdict = judge(tmp_path, "def solve(a, b):\n    return 999\n", CASES)
    assert not verdict.verified
    assert verdict.executed
    assert verdict.n_correct == 0
    assert verdict.note == "mismatch"


def test_forge_by_writing_fake_results_is_rejected(tmp_path):
    # Even if it finds and overwrites RESULTS, it does not know the expected values.
    source = (
        "import json, os\n"
        "def solve(a, b):\n"
        "    open(os.environ['CAIRNWORK_BBJ_RESULTS'], 'w').write(\n"
        "        json.dumps([{'ok': True, 'value': 0}, {'ok': True, 'value': 0},\n"
        "                    {'ok': True, 'value': 0}]))\n"
        "    return 0\n"
    )
    verdict = judge(tmp_path, source, CASES)
    assert not verdict.verified
    assert verdict.n_correct == 0


def test_expected_answers_are_not_reachable(tmp_path):
    # The candidate may read its own inputs, but the expected answers are never on disk, in the
    # environment or on stdin — only the supervisor holds them. Echoing the first arg fails.
    verdict = judge(tmp_path, "def solve(a, b):\n    return a\n", CASES)
    assert not verdict.verified
    assert verdict.n_correct == 0


def test_partial_correctness_is_not_a_pass(tmp_path):
    # Correct for the first case only -> not all correct -> rejected, with an honest count.
    source = "def solve(a, b):\n    return 2 if (a, b) == (1, 1) else -1\n"
    verdict = judge(tmp_path, source, CASES)
    assert not verdict.verified
    assert verdict.n_correct == 1
    assert verdict.n_total == 3


def test_raising_candidate_is_rejected_not_crashed(tmp_path):
    verdict = judge(tmp_path, "def solve(a, b):\n    raise ValueError('boom')\n", CASES)
    assert not verdict.verified
    assert verdict.executed  # the harness survived; the case is just wrong
    assert verdict.n_correct == 0


def test_network_is_denied_inside_the_judge(tmp_path):
    # A candidate that tries to reach the network cannot; the case fails rather than escaping.
    source = (
        "import socket\n"
        "def solve(a, b):\n"
        "    socket.create_connection(('127.0.0.1', 9), timeout=1)\n"
        "    return a + b\n"
    )
    verdict = judge(tmp_path, source, CASES)
    assert not verdict.verified
