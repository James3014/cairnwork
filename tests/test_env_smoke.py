"""The env smoke test tells an ENV_OK repo apart from one whose green tests break only
under Cairnwork's sandbox (ENV_COUPLING), and refuses a repo that is not green bare."""

import secrets
import sys
from pathlib import Path

import pytest
from conftest import commit_change

from scripts.env_smoke import RepoProbe, probe_repo

pytestmark = pytest.mark.sandbox


def _probe(repo, target):
    return probe_repo(RepoProbe(repo, (target,), sys.executable), timeout=120)


def test_green_tests_that_stay_green_are_env_ok(repo):
    commit_change(repo, files={"tests/test_ok.py": "def test_x():\n    assert 1 + 1 == 2\n"})
    assert _probe(repo, "tests/test_ok.py").verdict == "ENV_OK"


def test_tests_that_fail_bare_are_not_a_probe(repo):
    commit_change(repo, files={"tests/test_bad.py": "def test_x():\n    assert False\n"})
    assert _probe(repo, "tests/test_bad.py").verdict == "REPO_TESTS_NOT_GREEN"


def test_write_outside_the_repo_shows_up_as_env_coupling(repo):
    # Passes bare (it can write to /private/tmp) but the sandbox denies that write, so the
    # same green test fails only under the real verification path -> ENV_COUPLING.
    escape = Path("/private/tmp") / f"cairnwork_env_smoke_{secrets.token_hex(8)}.txt"
    source = f"def test_x():\n    open({str(escape)!r}, 'w').write('x')\n    assert True\n"
    commit_change(repo, files={"tests/test_escape.py": source})
    try:
        result = _probe(repo, "tests/test_escape.py")
    finally:
        escape.unlink(missing_ok=True)
    assert result.verdict == "ENV_COUPLING"
