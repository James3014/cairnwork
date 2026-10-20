"""Witness: a real Nexus-new test file through run_verifiers, as the verifier account.

Needs a clean Nexus-new clone and its venv (scripts/setup_target_venv.sh); set
CAIRNWORK_NEXUS_NEW_CLONE and CAIRNWORK_NEXUS_NEW_PYTHON. Skipped otherwise, also
on the macOS CI job, which has neither.
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from conftest import git

from cairnwork.verifier_account import VerifierAccount
from cairnwork.verifiers import parse_verifier, run_verifiers

CLONE = os.environ.get("CAIRNWORK_NEXUS_NEW_CLONE", "")
PYTHON = os.environ.get("CAIRNWORK_NEXUS_NEW_PYTHON", "")
TEST = "tests/services/test_federated_lessons.py"
MODULE = "nexus/services/federated_lessons.py"
GOOD, BAD = b'!= "lesson_event.v1"', b'!= "lesson_event.v0"'
ACCOUNT = VerifierAccount()

pytestmark = [
    pytest.mark.sandbox,
    pytest.mark.verifier_account,
    pytest.mark.skipif(not (CLONE and PYTHON), reason="needs a prepared Nexus-new clone and venv"),
]


def with_file(repo: Path, base: str, path: str, data: bytes) -> str:
    """A commit on top of base with one file replaced, made in a private index."""
    with tempfile.TemporaryDirectory() as scratch:
        env = {
            **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
            "GIT_INDEX_FILE": str(Path(scratch) / "index"),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }

        def run(*args, data=None):
            return (
                subprocess.run(
                    ["git", "-C", str(repo), *args],
                    input=data,
                    env=env,
                    capture_output=True,
                    check=True,
                )
                .stdout.decode()
                .strip()
            )

        run("read-tree", base)
        oid = run("hash-object", "-w", "--stdin", data=data)
        run("update-index", "--cacheinfo", f"100644,{oid},{path}")
        return run("commit-tree", run("write-tree"), "-p", base, "-m", "witness")


def verify(repo: Path, commit: str, base: str) -> bool:
    spec = parse_verifier("lessons", ["python3", "-m", "pytest", TEST])
    run = run_verifiers(
        repo,
        commit,
        (spec,),
        ACCOUNT.run_root,
        "w" + os.urandom(6).hex(),
        account=ACCOUNT,
        python=PYTHON,
        base_commit=base,
    )[0]
    ACCOUNT.remove(run.log_path.parent)
    shutil.rmtree(run.log_path.parent, ignore_errors=True)
    return run.observation.passed


def test_a_nexus_new_test_passes_and_a_broken_copy_fails():
    repo = Path(CLONE)
    base = git(repo, "rev-parse", "origin/main^{commit}")
    source = git(repo, "show", f"{base}:{MODULE}").encode() + b"\n"
    assert source.count(GOOD) == 1
    assert verify(repo, base, base)
    assert not verify(repo, with_file(repo, base, MODULE, source.replace(GOOD, BAD)), base)
