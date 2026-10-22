import sys
import time
from dataclasses import replace

import pytest
from conftest import commit_change, git

from cairnwork.core_request import AcceptanceSpec
from cairnwork.notary import Notary
from cairnwork.verifiers import VerifierRejected, parse_verifier, run_verifiers

PYTEST = ["python3", "-m", "pytest"]

pytestmark = pytest.mark.sandbox


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


@pytest.mark.parametrize(
    "files,target",
    [
        ({"pytest.ini": "[pytest]\naddopts = --ignore=tests\n"}, "tests/test_a.py::test_x"),
        ({"src/src_a.py": "import os\nos._exit(0)\n"}, "tests/test_a.py::test_x"),
        (
            {"tests/test_skip.py": "import pytest\n@pytest.mark.skip\ndef test_s():\n    pass\n"},
            "tests/test_skip.py",
        ),
        ({"tests/test_none.py": "x = 1\n"}, "tests/test_none.py"),
        (
            {
                "tests/test_ax.py": "import atexit, os\natexit.register(lambda: os._exit(0))\n"
                "def test_f():\n    assert False\n"
            },
            "tests/test_ax.py",
        ),
        (
            {
                "tests/test_xf.py": "import pytest\npytestmark = pytest.mark.xfail\n"
                "def test_f():\n    assert False\n"
            },
            "tests/test_xf.py",
        ),
        (
            {
                "tests/conftest.py": "def pytest_sessionfinish(session, exitstatus):\n"
                "    session.exitstatus = 0\n",
                "tests/test_cf.py": "def test_f():\n    assert False\n",
            },
            "tests/test_cf.py",
        ),
        (
            {
                "setup.cfg": "[tool:pytest]\naddopts = --co\n",
                "tests/test_co.py": "def test_f():\n    assert False\n",
            },
            "tests/test_co.py",
        ),
    ],
    ids=[
        "addopts-hijack",
        "exit-0-on-import",
        "all-skipped",
        "no-tests",
        "atexit-exit-0",
        "xfail-all",
        "conftest-exitstatus",
        "setup-cfg-collect-only",
    ],
)
def test_runs_that_do_not_really_pass_tests_fail(project, tmp_path, files, target):
    head = commit_change(project, files)
    result = run(project, tmp_path, head, target)
    assert not result.observation.passed


def test_background_processes_are_killed_after_the_run(project, tmp_path):
    marker = tmp_path / "late-write"
    child = f"import time, pathlib; time.sleep(2); pathlib.Path({str(marker)!r}).write_text('x')"
    head = commit_change(
        project,
        {
            "tests/test_bg.py": (
                "import subprocess, sys\n"
                "def test_spawn():\n"
                f"    subprocess.Popen([sys.executable, '-c', {child!r}])\n"
            )
        },
    )
    assert run(project, tmp_path, head, "tests/test_bg.py").observation.passed
    time.sleep(3)
    assert not marker.exists()


def test_logs_are_never_written_through_a_symlink(tmp_path):
    from cairnwork.verifiers import _write_new

    target = tmp_path / "victim"
    target.write_text("keep")
    (tmp_path / "log").symlink_to(target)
    with pytest.raises(OSError):
        _write_new(tmp_path / "log", b"overwrite")
    assert target.read_text() == "keep"


def test_protected_verifier_inputs():
    from cairnwork.verifiers import protected_verifier_inputs

    paths = (
        "src/a.py",
        "tests/test_a.py",
        "pkg/conftest.py",
        "pyproject.toml",
        "setup.cfg",
        "pytest.py",
        "evil.pth",
        "src/sitecustomize.py",
    )
    assert protected_verifier_inputs(paths) == paths[1:]


def test_ini_in_a_parent_directory_is_ignored(project, tmp_path):
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "pytest.ini").write_text("[pytest]\naddopts = --co\n")
    result = run(project, tmp_path, git(project, "rev-parse", "HEAD"), "tests/test_a.py::test_x")
    assert not result.observation.passed


def test_duplicate_verifier_ids_are_rejected(project, tmp_path):
    spec = parse_verifier("pytest", PYTEST + ["tests/test_a.py"])
    with pytest.raises(VerifierRejected):
        run_verifiers(project, git(project, "rev-parse", "HEAD"), (spec, spec), tmp_path, "r1")


def test_output_over_the_cap_fails(project, tmp_path, monkeypatch):
    import cairnwork.verifiers as verifiers

    head = commit_change(project, {"src/src_a.py": "X = 2\n"})
    assert run(project, tmp_path / "ok", head, "tests/test_a.py::test_x").observation.passed
    monkeypatch.setattr(verifiers, "MAX_LOG_BYTES", 10)
    assert not run(project, tmp_path / "big", head, "tests/test_a.py::test_x").observation.passed


def test_observation_hash_binds_the_candidate_commit(project, tmp_path, monkeypatch):
    import cairnwork.verifiers as verifiers

    monkeypatch.setattr(verifiers, "_file_sha256", lambda _path: "same-log")
    first = commit_change(project, {"src/src_a.py": "X = 2\n"})
    second = commit_change(project, {"src/other.py": "Y = 1\n"})
    one = run(project, tmp_path / "1", first, "tests/test_a.py::test_x").observation
    two = run(project, tmp_path / "2", second, "tests/test_a.py::test_x").observation
    assert one.passed and two.passed
    assert one.artifact_hash != two.artifact_hash


def test_root_pytest_module_cannot_replace_pytest(project, tmp_path):
    fake = (
        "import sys\n"
        "for arg in sys.argv:\n"
        "    if arg.startswith('--junitxml='):\n"
        "        open(arg.split('=', 1)[1], 'w').write(\n"
        '            \'<testsuite tests="1" failures="0" errors="0" skipped="0"/>\')\n'
        "sys.exit(0)\n"
    )
    head = commit_change(
        project, {"pytest.py": fake, "tests/test_fake.py": "def test_f():\n    assert False\n"}
    )
    assert not run(project, tmp_path, head, "tests/test_fake.py").observation.passed


def test_candidate_only_reachable_from_attempt_refs_can_be_verified(project, tmp_path):
    base = git(project, "rev-parse", "HEAD")
    tree_commit = commit_change(project, {"src/src_a.py": "X = 2\n"})
    git(project, "update-ref", "refs/cairnwork/attempts/a1", tree_commit)
    git(project, "reset", "-q", "--hard", base)
    git(project, "reflog", "expire", "--expire=now", "--all")
    result = run(project, tmp_path, tree_commit, "tests/test_a.py::test_x")
    assert result.observation.passed


def test_artifact_id_points_at_the_log(project, tmp_path):
    result = run(project, tmp_path, git(project, "rev-parse", "HEAD"), "tests/test_a.py::test_x")
    assert (tmp_path / "runs" / result.observation.artifact_id) == result.log_path


# Issue #114: a run whose log shows passing tests failed with no recorded cause. Reading the
# process list is slow on a loaded host; one timed-out read counted as "a process escaped".
def test_unreadable_process_list_fails_closed_and_says_why(project, tmp_path, monkeypatch):
    import cairnwork.confined as confined

    monkeypatch.setattr(confined, "_processes_with_environment", lambda: None)
    head = commit_change(project, {"src/src_a.py": "X = 2\n"})
    result = run(project, tmp_path, head, "tests/test_a.py::test_x")
    assert result.returncode == 0 and b"passed" in result.log_path.read_bytes()
    assert not result.observation.passed and result.survivors == 1
    assert "outlived the run" in result.why


def test_a_passing_run_has_no_failure_reason(project, tmp_path):
    head = commit_change(project, {"src/src_a.py": "X = 2\n"})
    assert run(project, tmp_path, head, "tests/test_a.py::test_x").why == ""


def test_a_failing_test_says_which_exit_code(project, tmp_path):
    result = run(project, tmp_path, git(project, "rev-parse", "HEAD"), "tests/test_a.py::test_x")
    assert f"pytest exit code {result.returncode}" in result.why


class _NotedBackend:
    """The selected backend, but its policy carries a note (as bwrap does when shm is withheld)."""

    def __init__(self, real, notes):
        self._real, self._notes = real, notes

    def __getattr__(self, name):
        return getattr(self._real, name)

    def policy(self, *args, **kwargs):
        return replace(self._real.policy(*args, **kwargs), notes=self._notes)


def _with_notes(monkeypatch, notes):
    import cairnwork_sandbox

    real = cairnwork_sandbox.select()
    monkeypatch.setattr(cairnwork_sandbox, "select", lambda: _NotedBackend(real, notes))


def test_policy_notes_reach_the_run_and_the_reason_but_not_the_hash(project, tmp_path, monkeypatch):
    # #154: the operator sees why shm was withheld; the observation's hash inputs do not change
    # (per-run paths make two digests differ anyway, so compare what is hashed).
    from cairnwork import verifiers
    from cairnwork.reasons import explain

    seen = []
    real_digest = verifiers._record_digest
    monkeypatch.setattr(
        verifiers, "_record_digest", lambda **kw: seen.append(kw) or real_digest(**kw)
    )
    head = commit_change(project, {"src/src_a.py": "X = 2\n"})
    plain = run(project, tmp_path / "a", head, "tests/test_a.py::test_x")
    note = "shm withheld: BWRAP_TMPFS_SIZE_UNSUPPORTED"
    _with_notes(monkeypatch, (note,))
    noted = run(project, tmp_path / "b", head, "tests/test_a.py::test_x")
    assert plain.notes == () and noted.notes == (note,)
    assert noted.observation.passed and explain([noted]) == ""
    assert seen[0].keys() == seen[1].keys()
    assert not any(note in str(value) for value in seen[1].values())
    failing = run(project, tmp_path / "c", head, "tests/test_a.py::test_writes")
    assert failing.notes == (note,) and f"({note})" in explain([failing])
