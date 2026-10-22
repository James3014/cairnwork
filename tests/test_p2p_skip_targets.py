"""#176: P2P targets are chosen per test (node): a skip or xfail on the base hides no test."""

import hashlib

import pytest
from conftest import commit_change
from test_p2p import CALC, CONFTEST, FIX_ADD, run

pytestmark = pytest.mark.sandbox

MIXED = (
    "import pytest\nfrom calc import neg\n"
    "@pytest.mark.skip(reason='x')\ndef test_skipped():\n    assert False\n"
    "@pytest.mark.xfail\ndef test_xf():\n    assert False\n"
    "def test_neg():\n    assert neg(2) == -2\n"
)
BREAK_NEG = "SLOW = False\ndef add(a, b):\n    return a + b\ndef neg(a):\n    return a\n"
SKIP_NEG = (
    "SLOW = False\ndef add(a, b):\n    return a + b\n"
    "def neg(a):\n    import pytest; pytest.skip('x')\n"
)  # fmt: skip
ONLY_PASSING = "tests/test_mixed.py::test_neg"


@pytest.fixture
def project(repo):
    commit_change(
        repo, {"src/calc.py": CALC, "tests/conftest.py": CONFTEST, "tests/test_mixed.py": MIXED}
    )
    return repo


@pytest.mark.parametrize("mode", ["whole-suite", "module-dir"])
def test_the_targets_are_exactly_the_passing_test_of_a_file_with_a_skip_and_an_xfail(
    project, tmp_path, mode
):
    result = run(project, tmp_path, {"src/calc.py": FIX_ADD}, mode=mode)
    assert result.status == "COMPLETED", result.reason
    assert result.p2p["targets"] == 1
    assert result.p2p["targets_sha256"] == hashlib.sha256(ONLY_PASSING.encode()).hexdigest()


@pytest.mark.parametrize("fix", [BREAK_NEG, SKIP_NEG], ids=["break", "turn-into-skip"])
def test_a_candidate_that_breaks_or_skips_that_test_is_blocked(project, tmp_path, fix):
    result = run(project, tmp_path, {"src/calc.py": fix}, mode="whole-suite")
    assert result.status == "FAILED" and not result.verified
    assert result.p2p["targets"] == 1
