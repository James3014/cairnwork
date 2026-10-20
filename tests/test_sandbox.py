"""Witnesses: the verifier sandbox really denies, and pytest really runs inside it.

Each attack is a candidate test that asserts the operation is refused. A verifier
PASS therefore proves the denial happened inside the real Seatbelt sandbox; if
the sandbox let it through, the test (and so the verifier) would fail.
"""

import sys

import pytest
from conftest import commit_change, git

from cairnwork import sandbox
from cairnwork.verifiers import parse_verifier, run_verifiers

pytestmark = pytest.mark.sandbox


@pytest.fixture
def project(repo):
    commit_change(
        repo,
        {
            "tests/conftest.py": (
                "import sys, pathlib\n"
                "sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / 'src'))\n"
            ),
            "tests/test_ok.py": "def test_ok():\n    assert 1 + 1 == 2\n",
        },
    )
    return repo


def verify(project, tmp_path, files, target):
    head = commit_change(project, files) if files else git(project, "rev-parse", "HEAD")
    spec = parse_verifier("pytest", ["python3", "-m", "pytest", target])
    runs = run_verifiers(project, head, (spec,), tmp_path / "runs", "r1", python=sys.executable)
    return runs[0]


def test_real_pytest_passes_inside_the_sandbox(project, tmp_path):
    result = verify(project, tmp_path, {}, "tests/test_ok.py")
    assert result.observation.passed, result.log_path.read_text()


def attack(code):
    return "import os, socket, pytest, subprocess\n\ndef test_denied():\n" + "".join(
        f"    {line}\n" for line in code.splitlines()
    )


@pytest.mark.parametrize(
    "name,code",
    [
        (
            "network",
            "with pytest.raises(OSError):\n"
            "    socket.create_connection(('1.1.1.1', 80), timeout=3)",
        ),
        (
            "curl",
            "result = subprocess.run(['/usr/bin/curl', '-s', '-m', '3', 'http://1.1.1.1'])\n"
            "assert result.returncode != 0",
        ),
        (
            "read-outside",
            "with pytest.raises(PermissionError):\n    open(os.environ['CW_SECRET']).read()",
        ),
        (
            "write-outside",
            "with pytest.raises(PermissionError):\n    open(os.environ['CW_OUTSIDE'], 'w')",
        ),
        (
            "write-tmp",
            "with pytest.raises(PermissionError):\n    open(os.environ['CW_TMP_TARGET'], 'w')",
        ),
        (
            "read-ssh",
            "with pytest.raises(PermissionError):\n    os.listdir(os.environ['CW_SSH'])",
        ),
        (
            "read-repo-git",
            "with pytest.raises(PermissionError):\n    os.listdir(os.environ['CW_REPO_GIT'])",
        ),
    ],
)
def test_attacks_are_denied(project, tmp_path, monkeypatch, name, code):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET\n")
    import os
    import pathlib
    import uuid

    monkeypatch.setenv("CW_SECRET", str(secret))
    monkeypatch.setenv("CW_OUTSIDE", str(tmp_path / "escaped.txt"))
    monkeypatch.setenv("CW_SSH", str(pathlib.Path.home() / ".ssh"))
    tmp_target = f"/tmp/cairnwork-escape-{uuid.uuid4().hex}"
    monkeypatch.setenv("CW_TMP_TARGET", tmp_target)
    monkeypatch.setenv("CW_REPO_GIT", str(project / ".git"))
    import cairnwork.verifiers as verifiers

    monkeypatch.setattr(
        verifiers,
        "_ENV_KEEP",
        (*verifiers._ENV_KEEP, "CW_SECRET", "CW_OUTSIDE", "CW_SSH", "CW_REPO_GIT", "CW_TMP_TARGET"),
    )
    result = verify(
        project,
        tmp_path,
        {f"tests/test_{name.replace('-', '_')}.py": attack(code)},
        f"tests/test_{name.replace('-', '_')}.py",
    )
    assert result.observation.passed, result.log_path.read_text()
    assert not (tmp_path / "escaped.txt").exists()
    assert not os.path.exists(tmp_target)


def test_without_sandbox_nothing_runs_and_the_verifier_fails(project, tmp_path, monkeypatch):
    marker = tmp_path / "ran"
    monkeypatch.setattr(sandbox, "available", lambda: False)
    result = verify(
        project,
        tmp_path,
        {"tests/test_mark.py": f"def test_m():\n    open({str(marker)!r}, 'w').write('x')\n"},
        "tests/test_mark.py",
    )
    assert not result.observation.passed
    assert not marker.exists()
    assert "unavailable" in result.log_path.read_text()


def test_child_cannot_touch_its_own_log_ini_or_profile(project, tmp_path):
    code = (
        "import pathlib, pytest\n"
        "def test_tamper():\n"
        "    run = pathlib.Path(__import__('os').environ['HOME']).parent\n"
        "    for name in ('verifier.log', 'pytest.ini', 'profile.sb'):\n"
        "        with pytest.raises(PermissionError):\n"
        "            open(run.parent / name, 'a').write('x')\n"
    )
    result = verify(project, tmp_path, {"tests/test_tamper.py": code}, "tests/test_tamper.py")
    assert result.observation.passed, result.log_path.read_text()


@pytest.mark.parametrize("bad", ['/tmp/a"b', "/tmp/a(b", "/tmp/a\\\\b", "/tmp/a\nb", "/tmp/é"])
def test_profile_paths_cannot_rewrite_the_policy(bad):
    with pytest.raises(sandbox.SandboxError):
        sandbox.sbpl_path(bad)


def test_profile_resolves_symlinked_paths(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    profile = sandbox.build_profile((), (tmp_path / "link",), home=tmp_path)
    assert str(real.resolve()) in profile.text
    assert "(deny network*)" in profile.text and "(deny default)" in profile.text
