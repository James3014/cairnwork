"""Shared helpers and fixtures for the cairnwork-verify tests (registered in conftest)."""

import json
import subprocess
import sys

import pytest
from conftest import commit_change, git

VERIFY = "python3 -m pytest -q tests/test_calc.py"
FIX = "def add(a, b):\n    return a + b\n"


@pytest.fixture
def project(repo):
    commit_change(
        repo,
        {
            "src/calc.py": "def add(a, b):\n    return a + b + 1\n",
            "tests/conftest.py": (
                "import sys, pathlib\n"
                "sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / 'src'))\n"
            ),
            "tests/test_calc.py": (
                "from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n"
            ),
            "tests/test_skip.py": (
                "import pytest\ndef test_ok():\n    pass\n"
                "@pytest.mark.skip\ndef test_skipped():\n    pass\n"
            ),
        },
    )
    return repo


@pytest.fixture
def state(tmp_path):
    path = tmp_path / "state"
    path.mkdir(mode=0o700)
    (path / "config.json").write_text(json.dumps({"verifier_account": False}))
    (path / "ACCEPTING").touch()
    return path


def candidate(project, base, files=None, delete=()):
    """One commit on base, kept on a branch so a clone can reach it."""
    git(project, "checkout", "-q", "--detach", base)
    sha = commit_change(project, files, delete=delete)
    git(project, "branch", f"c-{sha[:8]}", sha)
    git(project, "checkout", "-q", "main")
    return sha


def verify(state, project, base, cand, *extra, allowed=("src/calc.py",), tests=(VERIFY,)):
    args = ["--repo-path", str(project), "--base", base, "--candidate", cand]
    for name in allowed:
        args += ["--allowed-file", name]
    for command in tests:
        args += ["--verifier", command]
    done = subprocess.run(
        [sys.executable, "-I", "-m", "cairnwork.verify_cli", "--state", str(state), "run", *args]
        + list(extra),
        capture_output=True, text=True, timeout=300, check=False,
    )  # fmt: skip
    return done.returncode, json.loads(done.stdout)


def base_of(project):
    return git(project, "rev-parse", "main")


def cli(state, *args):
    done = subprocess.run(
        [sys.executable, "-I", "-m", "cairnwork.verify_cli", "--state", str(state), *args],
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    return done.returncode, json.loads(done.stdout)


def make_mirror(project, tmp_path):
    """Cairnwork's own repository for the target: a clone made before any candidate exists,
    with the project's origin (where the deferred tests are published)."""
    mirror = tmp_path / "mirror"
    git(tmp_path, "clone", "-q", "--no-local", str(project), str(mirror))
    origin = subprocess.run(
        ["git", "-C", str(project), "remote", "get-url", "origin"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()  # fmt: skip
    if origin:
        git(mirror, "remote", "set-url", "origin", origin)
    return mirror


def has_object(repo, sha):
    done = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"], check=False
    )
    return done.returncode == 0


def in_process(project, state, base, cand):
    from cairnwork.intake import Intake
    from cairnwork.verify_candidate import verify_candidate

    return verify_candidate(
        Intake(state),
        repo=project,
        base=base,
        candidate=cand,
        allowed_files=("src/calc.py",),
        verifiers=(("v", tuple(VERIFY.split())),),
    )
