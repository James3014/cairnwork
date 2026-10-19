import sys

import pytest
from conftest import commit_change, git

from cairnwork.core_request import AcceptanceSpec
from cairnwork.notary import Notary
from cairnwork.verifiers import VerifierRejected, parse_verifier, run_verifiers

PYTEST = ["python3", "-m", "pytest"]


@pytest.mark.parametrize(
    "argv",
    [
        PYTEST + ["tests/test_a.py"],
        PYTEST + ["-q", "tests/test_a.py::test_x"],
        PYTEST + ["-q", "-p", "no:cacheprovider", "tests/sub/test_b.py", "tests/test_a.py"],
    ],
)
def test_allowed_verifiers_parse(argv):
    assert parse_verifier("pytest", argv).targets


@pytest.mark.parametrize(
    "argv",
    [
        ["bash", "-c", "pytest"],
        ["python3", "-m", "evil", "tests/test_a.py"],
        ["/tmp/python3", "-m", "pytest", "tests/test_a.py"],
        ["python3", "-c", "print(1)"],
        PYTEST,
        PYTEST + ["tests/../secret.py"],
        PYTEST + ["/abs/tests/test_a.py"],
        PYTEST + ["src/a.py"],
        PYTEST + ["-k", "x", "tests/test_a.py"],
        PYTEST + ["-p", "evil_plugin", "tests/test_a.py"],
        PYTEST + ["--rootdir=/", "tests/test_a.py"],
        PYTEST + ["tests/test_a.py; rm -rf /"],
        "python3 -m pytest tests/test_a.py",
    ],
)
def test_other_verifiers_are_rejected(argv):
    with pytest.raises(VerifierRejected):
        parse_verifier("pytest", argv)


@pytest.fixture
def project(repo):
    commit_change(
        repo,
        {
            "tests/test_a.py": (
                "import os, pathlib, time\n"
                "from src_a import X\n"
                "def test_x():\n    assert X == 2\n"
                "def test_env():\n    assert 'CAIRNWORK_SECRET' not in os.environ\n"
                "def test_writes():\n    pathlib.Path('written.txt').write_text('x')\n"
                "def test_slow():\n    time.sleep(30)\n"
            ),
            "tests/conftest.py": (
                "import sys, pathlib\n"
                "sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / 'src'))\n"
            ),
            "src/src_a.py": "X = 1\n",
        },
    )
    return repo


def run(repo, tmp_path, commit, target, **kwargs):
    spec = parse_verifier("pytest", PYTEST + [target])
    return run_verifiers(
        repo, commit, (spec,), tmp_path / "runs", "r1", python=sys.executable, **kwargs
    )[0]


def test_failing_test_is_not_passed(project, tmp_path):
    result = run(project, tmp_path, git(project, "rev-parse", "HEAD"), "tests/test_a.py::test_x")
    assert result.returncode != 0 and not result.observation.passed


def test_passing_test_is_passed_and_logged(project, tmp_path):
    head = commit_change(project, {"src/src_a.py": "X = 2\n"})
    result = run(project, tmp_path, head, "tests/test_a.py::test_x")
    assert result.returncode == 0 and result.observation.passed
    assert result.log_path.read_bytes()
    assert result.observation.artifact_hash.startswith("sha256:")


def test_inherited_secrets_do_not_reach_the_verifier(project, tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRNWORK_SECRET", "x")
    head = git(project, "rev-parse", "HEAD")
    assert run(project, tmp_path, head, "tests/test_a.py::test_env").observation.passed


def test_verifier_that_writes_into_the_checkout_fails(project, tmp_path):
    result = run(
        project, tmp_path, git(project, "rev-parse", "HEAD"), "tests/test_a.py::test_writes"
    )
    assert result.returncode == 0
    assert result.mutated and not result.observation.passed


def test_timeout_fails(project, tmp_path):
    result = run(
        project,
        tmp_path,
        git(project, "rev-parse", "HEAD"),
        "tests/test_a.py::test_slow",
        timeout=2,
    )
    assert result.timed_out and not result.observation.passed


def test_checkouts_are_removed_after_the_run(project, tmp_path):
    run(project, tmp_path, git(project, "rev-parse", "HEAD"), "tests/test_a.py::test_x")
    assert not (tmp_path / "runs" / "r1-v0").exists()


def test_verifier_observations_lead_to_core_verified(project, tmp_path):
    base = git(project, "rev-parse", "HEAD")
    head = commit_change(project, {"src/src_a.py": "X = 2\n"})
    result = run(project, tmp_path, head, "tests/test_a.py::test_x")
    spec = AcceptanceSpec("task-1", "sha256:" + "c" * 64, ("pytest",), ("src/src_a.py",))
    notary = Notary(tmp_path / "notary.sqlite")
    verdict = notary.notarize("task-1", "a1", spec, project, base, head, (result.observation,))
    assert verdict.verified
