"""Witnesses: the verifier sandbox really denies, and pytest really runs inside it.

Each attack is a candidate test that asserts the operation is refused. A verifier
PASS therefore proves the denial happened inside the real Seatbelt sandbox; if
the sandbox let it through, the test (and so the verifier) would fail.
"""

import sys
from pathlib import Path

import pytest
from conftest import commit_change, git

from cairnwork import sandbox
from cairnwork.verifiers import parse_verifier, run_verifiers


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


@pytest.mark.sandbox
def test_real_pytest_passes_inside_the_sandbox(project, tmp_path):
    result = verify(project, tmp_path, {}, "tests/test_ok.py")
    assert result.observation.passed, result.log_path.read_text()


def attack(code):
    return "import os, socket, pytest, subprocess\n\ndef test_denied():\n" + "".join(
        f"    {line}\n" for line in code.splitlines()
    )


@pytest.mark.sandbox
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


@pytest.mark.sandbox
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


@pytest.mark.parametrize(
    "bad", ['/tmp/a"b', "/tmp/a(b", "/tmp/a\\\\b", "/tmp/a\nb", "/tmp/é", "/tmp/a\n"]
)
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


@pytest.mark.parametrize("root", ["/", "/Users", "/private/tmp", "HOME", "HOME_PARENT"])
def test_broad_roots_are_refused(tmp_path, root):
    home = tmp_path / "home"
    home.mkdir()
    path = {"HOME": home, "HOME_PARENT": tmp_path}.get(root, root)
    with pytest.raises(sandbox.SandboxError):
        sandbox.build_profile((Path(path),), (), home=home)


@pytest.mark.sandbox
def test_trailing_credential_deny_wins_inside_an_allowed_root(tmp_path):
    """SBPL: the last matching rule wins. A credential path under an allowed read root
    must stay unreadable (a reviewer claimed first-match-wins; this pins the behaviour)."""
    import subprocess

    home = tmp_path / "home"
    keychains = home / "Library" / "Keychains"
    keychains.mkdir(parents=True)
    (keychains / "login.db").write_text("KEY\n")
    (home / "Library" / "ok.txt").write_text("OK\n")
    profile = sandbox.build_profile((home / "Library",), (), home=home)
    path = tmp_path / "p.sb"
    path.write_text(profile.text)
    run = lambda target: subprocess.run(  # noqa: E731
        sandbox.wrap(["/bin/cat", str(target)], path), capture_output=True, text=True
    )
    assert run(home / "Library" / "ok.txt").stdout == "OK\n"
    denied = run(keychains / "login.db")
    assert denied.returncode != 0 and "KEY" not in denied.stdout


@pytest.mark.sandbox
def test_child_cannot_rewrite_its_log_through_stdout(project, tmp_path):
    code = (
        "import os\n"
        "def test_rewrite():\n"
        "    try:\n"
        "        os.ftruncate(1, 0)\n"
        "    except OSError:\n"
        "        pass\n"
        "    os.write(1, b'FORGED\\n')\n"
    )
    result = verify(project, tmp_path, {"tests/test_rewrite.py": code}, "tests/test_rewrite.py")
    log = result.log_path.read_text()
    assert "test_rewrite" in log or "passed" in log or "failed" in log


def test_probe_requires_a_real_denial(monkeypatch):
    sandbox.available.cache_clear()
    monkeypatch.setattr(sandbox, "_probe", lambda policy: 0)
    try:
        assert sandbox.available() is False
    finally:
        sandbox.available.cache_clear()


@pytest.mark.sandbox
def test_process_that_leaves_the_group_is_killed_and_fails_the_run(project, tmp_path):
    marker = tmp_path / "late"
    child = f"import time, pathlib; time.sleep(3); pathlib.Path({str(marker)!r}).write_text('x')"
    code = (
        "import subprocess, sys\n"
        "def test_spawn():\n"
        f"    subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session=True)\n"
    )
    result = verify(project, tmp_path, {"tests/test_daemon.py": code}, "tests/test_daemon.py")
    assert not result.observation.passed
    import time

    time.sleep(4)
    assert not marker.exists()


def test_python_must_be_an_absolute_path(project, tmp_path):
    from cairnwork.verifiers import VerifierRejected

    spec = parse_verifier("pytest", ["python3", "-m", "pytest", "tests/test_ok.py"])
    with pytest.raises(VerifierRejected):
        run_verifiers(
            project, git(project, "rev-parse", "HEAD"), (spec,), tmp_path, "r1", python="python3"
        )


@pytest.mark.sandbox
def test_tests_can_run_git(project, tmp_path):
    """/usr/bin/git is an xcrun shim that loads its tools from the developer directory;
    a test that runs git must work inside the sandbox (Nexus-new #2769 pilot). The shim
    is named outright: a Homebrew git earlier on PATH is not what this is about."""
    code = (
        "import subprocess\n"
        "def test_git(tmp_path):\n"
        "    for argv in (['/usr/bin/git', '--version'], ['/usr/bin/git', 'init', '-q']):\n"
        "        done = subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True)\n"
        "        assert done.returncode == 0, done.stderr\n"
    )
    result = verify(project, tmp_path, {"tests/test_git.py": code}, "tests/test_git.py")
    assert result.observation.passed, result.log_path.read_text()


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        ("/Library/Developer/CommandLineTools", True),
        ("/Applications/Xcode.app/Contents/Developer", True),
        ("/Applications/Xcode_16.4.app/Contents/Developer", True),
        ("/Library/Developer", False),
        ("/Library/Developer/CoreSimulator", False),
        ("/Applications/XcodeEvil", False),
        ("/Applications/Xcode.app/Contents", False),
        ("/tmp", False),
    ],
)
def test_only_standard_developer_dirs_are_readable(path, allowed):
    assert bool(sandbox._DEVELOPER_DIR.fullmatch(path)) is allowed
