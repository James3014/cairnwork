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


def test_bare_side_puts_the_venv_python_on_path(repo, monkeypatch):
    # A repo whose test spawns plain ``python`` must pass bare too: the venv bin leads PATH on
    # both sides (verifiers #33). With PATH stripped to the system dirs, the fix is what makes
    # this ENV_OK instead of a false REPO_TESTS_NOT_GREEN.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    # plain "python": absent from /usr/bin on macOS, so only the prepended venv bin resolves it.
    source = (
        "import subprocess\n"
        "def test_x():\n"
        "    subprocess.run(['python', '-c', 'import sys'], check=True)\n"
    )
    commit_change(repo, files={"tests/test_py.py": source})
    assert _probe(repo, "tests/test_py.py").verdict == "ENV_OK"


def test_a_skipped_test_is_reported_as_skipped_not_coupling(repo):
    # The verifier rejects any skip (so a candidate cannot hide a failure behind one), so an
    # all-pass-but-skipped suite is not an env coupling — it should read as SKIPPED_TESTS.
    source = (
        "import pytest\n"
        "def test_ok():\n    assert True\n"
        "def test_s():\n    pytest.skip('platform')\n"
    )
    commit_change(repo, files={"tests/test_skip.py": source})
    result = _probe(repo, "tests/test_skip.py")
    assert result.verdict == "SKIPPED_TESTS"
    assert "1 skipped" in result.reason


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
