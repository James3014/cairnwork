"""Witnesses: the verifier sandbox really denies, and pytest really runs inside it.

Each attack is a candidate test that asserts the operation is refused. A verifier
PASS therefore proves the denial happened inside the real sandbox (Seatbelt on macOS,
bubblewrap on Linux); if the sandbox let it through, the test (and so the verifier)
would fail. The witnesses assert behaviour, not policy text; one backend's policy
construction is tested in test_seatbelt_profile.py and test_bwrap_probe.py.
"""

import sys

import pytest
from conftest import commit_change, git

import cairnwork_sandbox
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


_SEATBELT_ONLY = ("read-outside", "read-repo-git")


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
            "read-outside",  # seatbelt only: the bwrap root is readable (docs/SECURITY.md)
            "with pytest.raises(PermissionError):\n    open(os.environ['CW_SECRET']).read()",
        ),
        (
            "write-outside",
            "with pytest.raises(OSError):\n    open(os.environ['CW_OUTSIDE'], 'w')",
        ),
        (
            "write-tmp",
            "with pytest.raises(OSError):\n    open(os.environ['CW_TMP_TARGET'], 'w')",
        ),
        (
            "read-ssh",
            "try:\n    listing = os.listdir(os.environ['CW_SSH'])\n"
            "except OSError:\n    listing = []\n"
            "assert listing == []",
        ),
        (
            "read-repo-git",
            "with pytest.raises(PermissionError):\n    os.listdir(os.environ['CW_REPO_GIT'])",
        ),
    ],
)
def test_attacks_are_denied(project, tmp_path, monkeypatch, name, code):
    backend = cairnwork_sandbox.platform_backend()
    if name in _SEATBELT_ONLY and (backend is None or backend.name != "seatbelt"):
        pytest.skip("bwrap binds the whole root read-only: reads outside the roots are allowed")
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
    monkeypatch.setattr(cairnwork_sandbox.platform_backend(), "available", lambda: False)
    result = verify(
        project,
        tmp_path,
        {"tests/test_mark.py": f"def test_m():\n    open({str(marker)!r}, 'w').write('x')\n"},
        "tests/test_mark.py",
    )
    assert not result.observation.passed
    assert not marker.exists()
    assert "not executed" in result.log_path.read_text()  # backend-neutral: see verifiers.py


@pytest.mark.sandbox
def test_child_cannot_touch_its_own_log_ini_or_profile(project, tmp_path):
    code = (
        "import pathlib, pytest\n"
        "def test_tamper():\n"
        "    run = pathlib.Path(__import__('os').environ['HOME']).parent\n"
        "    for name in ('verifier.log', 'pytest.ini', 'profile.sb'):\n"
        "        with pytest.raises(OSError):\n"
        "            open(run.parent / name, 'a').write('x')\n"
    )
    result = verify(project, tmp_path, {"tests/test_tamper.py": code}, "tests/test_tamper.py")
    assert result.observation.passed, result.log_path.read_text()


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


@pytest.mark.sandbox
def test_a_process_that_leaves_the_group_is_contained(project, tmp_path):
    """A child that escapes the run's process group (start_new_session) must not outlive
    the run and do work. The backends reach that end differently: Seatbelt cannot reap an
    escaped session, so it sweeps the survivor by its run token and fails the run (so a
    worker that leaves a process behind gets no passing verdict); under bwrap the escapee
    stays in the PID namespace and is SIGKILLed when the namespace tears down, so nothing is
    left to sweep and the run may pass (see docs/SECURITY.md).

    This witness asserts the invariant both enforce: the escape writes no marker. The marker
    path is outside the write roots, so on bwrap the read-only root alone already forbids the
    write — this assertion does not by itself exercise the PID-namespace kill (the write-outside
    and no-network witnesses, plus the SECURITY.md reasoning, cover that). Making bwrap also
    fail the run, and a witness that proves the kill directly, are P3 follow-ups."""
    marker = tmp_path / "late"
    child = f"import time, pathlib; time.sleep(3); pathlib.Path({str(marker)!r}).write_text('x')"
    code = (
        "import subprocess, sys\n"
        "def test_spawn():\n"
        f"    subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session=True)\n"
    )
    result = verify(project, tmp_path, {"tests/test_daemon.py": code}, "tests/test_daemon.py")
    backend = cairnwork_sandbox.platform_backend()
    if backend is not None and backend.name == "seatbelt":
        assert not result.observation.passed  # the swept survivor fails the run
    import time

    time.sleep(4)
    assert not marker.exists()  # contained on either backend: the escape did no work


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


@pytest.mark.sandbox
def test_plain_python_is_the_verifier_interpreter(project, tmp_path, monkeypatch):
    """Tests that run plain ``python`` (Nexus-new #2760 pilot) get the verifier's own
    interpreter, as in an activated venv, even when PATH has no ``python`` at all."""
    code = (
        "import shutil, subprocess, sys\n"
        "from pathlib import Path\n"
        "def test_python():\n"
        "    found = shutil.which('python')\n"
        "    assert found and Path(found).parent == Path(sys.executable).parent\n"
        "    assert subprocess.run([found, '-c', 'pass']).returncode == 0\n"
    )
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    result = verify(project, tmp_path, {"tests/test_python.py": code}, "tests/test_python.py")
    assert result.observation.passed, result.log_path.read_text()


_NEXT_TO_REPO = (
    "from pathlib import Path\n"
    "repo = Path(__file__).resolve().parents[1]\n"
    "def test_next_to_repo():\n"
    "    target = repo.parent / 'nexus-runtime-targets'\n"
    "    target.mkdir()\n"
    "    (target / 'x').write_text('ok')\n"
    "def test_not_above_the_run():\n"
    "    try:\n"
    "        (repo.parent.parent / 'escaped').write_text('no')\n"
    "    except OSError:\n"
    "        return\n"
    "    raise AssertionError('wrote outside its work directory')\n"
)


@pytest.mark.sandbox
def test_tests_may_write_next_to_their_repo_but_not_above_the_run(project, tmp_path):
    """Nexus-new tests create <repo parent>/nexus-runtime-targets (cairnwork#38): that
    lands in the run's own work directory, which is removed afterwards."""
    result = verify(project, tmp_path, {"tests/test_next.py": _NEXT_TO_REPO}, "tests/test_next.py")
    assert result.observation.passed, result.log_path.read_text()
    runs = tmp_path / "runs"
    assert not (runs / "escaped").exists()
    assert not list(runs.glob("*.work")), "work directory left behind"


@pytest.mark.sandbox
def test_a_replaced_checkout_fails_the_run(project, tmp_path):
    """The child may write the work directory, so it could move its checkout away and put a
    symlink in its place; the owner then must not compare (or trust) another tree."""
    code = (
        "import os\n"
        "from pathlib import Path\n"
        "def test_swap():\n"
        "    repo = Path(__file__).resolve().parents[1]\n"
        "    os.rename(repo, repo.parent / 'moved')\n"
        "    os.symlink(repo.parent / 'moved', repo)\n"
    )
    result = verify(project, tmp_path, {"tests/test_swap.py": code}, "tests/test_swap.py")
    assert not result.observation.passed
    assert "checkout replaced" in result.mutated


@pytest.mark.sandbox
def test_named_credentials_are_masked(project, tmp_path, monkeypatch):
    """A fake home holds canary credentials; inside the sandbox none of them is readable
    (denied, missing or emptied), whichever backend confines the run."""
    home = tmp_path / "fakehome"
    canaries = [".ssh/id_test", ".aws/credentials", ".config/gh/hosts.yml", ".netrc", ".Xauthority"]
    for rel in canaries:
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        (home / rel).write_text("CANARY-SECRET\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CW_CANARIES", ":".join(str(home / rel) for rel in canaries))
    import cairnwork.verifiers as verifiers

    monkeypatch.setattr(verifiers, "_ENV_KEEP", (*verifiers._ENV_KEEP, "CW_CANARIES"))
    code = (
        "import os\n"
        "def test_masked():\n"
        "    paths = os.environ['CW_CANARIES'].split(':')\n"
        "    assert len(paths) == 5\n"
        "    for path in paths:\n"
        "        try:\n"
        "            data = open(path).read()\n"
        "        except OSError:\n"
        "            continue\n"
        "        assert 'CANARY-SECRET' not in data, path\n"
    )
    result = verify(project, tmp_path, {"tests/test_masked.py": code}, "tests/test_masked.py")
    assert result.observation.passed, result.log_path.read_text()


@pytest.mark.sandbox
def test_x11_and_daemon_sockets_are_masked(project, tmp_path, monkeypatch):
    """X11 server sockets and root-equivalent daemon sockets (libvirt/LXD/Docker, #85) are
    masked, so a candidate cannot reach one that exists on the host. The real paths vary and a
    CI runner has none, so point the (bwrap) mask lists at canary paths under tmp and witness
    them unreadable inside the sandbox (under bwrap's whole-root bind they would be readable
    without the mask; Seatbelt denies them by its allow-list anyway)."""
    import cairnwork_sandbox.bwrap as bwrap_mod

    x11 = tmp_path / ".X11-unix"
    x11.mkdir()
    (x11 / "X0").write_text("CANARY-X11\n")
    sock = tmp_path / "libvirt-sock"
    sock.write_text("CANARY-SOCK\n")
    monkeypatch.setattr(bwrap_mod, "_X11_SOCKET_DIR", str(x11))
    monkeypatch.setattr(bwrap_mod, "_DANGEROUS_SOCKETS", (str(sock),))
    monkeypatch.setenv("CW_X11", str(x11 / "X0"))
    monkeypatch.setenv("CW_SOCK", str(sock))
    import cairnwork.verifiers as verifiers

    monkeypatch.setattr(verifiers, "_ENV_KEEP", (*verifiers._ENV_KEEP, "CW_X11", "CW_SOCK"))
    code = (
        "import os\n"
        "def test_masked():\n"
        "    for key in ('CW_X11', 'CW_SOCK'):\n"
        "        try:\n"
        "            data = open(os.environ[key]).read()\n"
        "        except OSError:\n"
        "            continue\n"
        "        assert 'CANARY' not in data, key\n"
    )
    result = verify(project, tmp_path, {"tests/test_sock.py": code}, "tests/test_sock.py")
    assert result.observation.passed, result.log_path.read_text()
