"""The report-guard descriptor hand-over through the real sandbox (report_channel.py)."""

import sys

import pytest
from conftest import commit_change

from cairnwork.verifiers import parse_verifier, run_verifiers

pytestmark = pytest.mark.sandbox


def run(repo, tmp_path, commit, target):
    spec = parse_verifier("pytest", ["python3", "-m", "pytest", target])
    return run_verifiers(repo, commit, (spec,), tmp_path / "runs", "r1", python=sys.executable)[0]


FORGE_FROM_ENV = (
    "import os, json, atexit\n"
    "def test_x():\n    assert False\n"
    "def _forge():\n"
    "    p = os.environ.get('CAIRNWORK_VERDICT_PATH')\n"
    "    n = os.environ.get('CAIRNWORK_VERDICT_NONCE')\n"
    "    if p and n:\n"
    "        json.dump({'nonce': n, 'collected': ['tests/test_f.py::test_x'],\n"
    "                   'outcomes': {'tests/test_f.py::test_x': 'passed'}}, open(p, 'w'))\n"
    "atexit.register(_forge)\n"
)


def test_the_hand_over_is_not_in_the_environment_of_the_code_under_test(repo, tmp_path):
    """The owner path hands the nonce and report over by descriptor and the plugin scrubs the
    guard variables at import, so a test sees none of them (this also proves the descriptors
    reach the child through sandbox-exec and that the run is judged from them)."""
    head = commit_change(
        repo,
        {
            "tests/test_env_clean.py": (
                "import os\n"
                "def test_clean():\n"
                "    bad = [k for k in os.environ if 'VERDICT' in k or 'GUARD' in k]\n"
                "    assert not bad, bad\n"
            )
        },
    )
    result = run(repo, tmp_path, head, "tests/test_env_clean.py")
    assert result.returncode == 0 and result.observation.passed and result.ran
    assert result.outcomes == {"tests/test_env_clean.py::test_clean": "passed"}


def test_forging_a_pass_from_the_environment_no_longer_works(repo, tmp_path):
    head = commit_change(repo, {"tests/test_f.py": FORGE_FROM_ENV})
    result = run(repo, tmp_path, head, "tests/test_f.py")
    assert not result.observation.passed
    assert result.outcomes == {"tests/test_f.py::test_x": "failed"}
