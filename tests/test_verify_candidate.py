"""``cairnwork-verify``: judge a commit made elsewhere with the checks Cairnwork
applies to its own worker's output, without a worker and without a ledger record."""

import json
import subprocess
import sys

import pytest
from conftest import commit_change, git
from core_http_server import core_http_server
from test_deferred_tests import CHECK, REGRESSION, make_test_commit

from cairnwork.ownership import OwnershipLedger

sandbox = pytest.mark.sandbox
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
        [sys.executable, "-I", "-m", "cairnwork_verify.cli", "--state", str(state), "run", *args]
        + list(extra),
        capture_output=True, text=True, timeout=300, check=False,
    )  # fmt: skip
    return done.returncode, json.loads(done.stdout)


def base_of(project):
    return git(project, "rev-parse", "main")


def no_ledger_trace(state):
    ledger = state / "ownership.sqlite"
    assert not ledger.exists() or OwnershipLedger(ledger).in_flight() == []
    assert not (state / "requests").exists() or not list((state / "requests").iterdir())


@sandbox
def test_a_correct_candidate_is_verified_and_leaves_no_task_behind(project, state):
    base = base_of(project)
    cand = candidate(project, base, {"src/calc.py": FIX})
    code, out = verify(state, project, base, cand)
    assert code == 0, out
    assert out["verified"] is True and out["status"] == "VERIFIED"
    assert out["core_status"] == "VERIFIED" and out["reason"] == "CORE_VERIFIED"
    no_ledger_trace(state)
    route = subprocess.run(
        [sys.executable, "-I", "-m", "cairnwork.route", "--state", str(state), "in-flight"],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert json.loads(route.stdout)["count"] == 0


def test_a_file_outside_the_scope_is_refused(project, state):
    base = base_of(project)
    cand = candidate(project, base, {"src/calc.py": FIX, "src/other.py": "x = 1\n"})
    code, out = verify(state, project, base, cand)
    assert code == 2 and out["verified"] is False and out["status"] == "REFUSED"
    assert out["reason"].startswith("OUT_OF_SCOPE") and out["core_status"] == ""


def test_a_deletion_is_refused(project, state):
    base = base_of(project)
    cand = candidate(project, base, delete=("src/b.py",))
    code, out = verify(state, project, base, cand, allowed=("src/b.py",))
    assert code == 2 and out["status"] == "REFUSED"
    assert out["reason"].startswith("DELETION_FORBIDDEN")


def test_a_candidate_that_is_not_one_commit_on_the_base_is_refused(project, state):
    base = base_of(project)
    first = candidate(project, base, {"src/calc.py": FIX})
    git(project, "checkout", "-q", "--detach", first)
    second = commit_change(project, {"src/calc.py": FIX + "\n"})
    git(project, "branch", "two", second)
    git(project, "checkout", "-q", "main")
    code, out = verify(state, project, base, second)
    assert code == 2 and out["reason"].startswith("NOT_ONE_COMMIT_ON_BASE")


def test_malformed_input_is_a_usage_error_or_a_refusal(project, state):
    base = base_of(project)
    code, out = verify(state, project, base, "not-a-commit")
    assert code == 2 and out["error"] == "BAD_CANDIDATE"
    cand = candidate(project, base, {"src/calc.py": FIX})
    code, out = verify(state, project, base, cand, allowed=("tests/test_calc.py",))
    assert code == 2 and out["error"] == "PROTECTED_VERIFIER_INPUT"
    done = subprocess.run(
        [sys.executable, "-I", "-m", "cairnwork_verify.cli", "--state", str(state), "run"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert done.returncode == 3 and json.loads(done.stdout)["error"] == "USAGE"


@sandbox
def test_a_verifier_that_fails_is_not_verified(project, state):
    base = base_of(project)
    cand = candidate(project, base, {"src/calc.py": "def add(a, b):\n    return a - b\n"})
    code, out = verify(state, project, base, cand)
    assert code == 2 and out["verified"] is False and out["status"] == "FAILED"
    assert out["core_status"] != "VERIFIED"


@sandbox
def test_a_verifier_with_a_skipped_test_is_not_verified(project, state):
    base = base_of(project)
    cand = candidate(project, base, {"src/calc.py": FIX})
    code, out = verify(
        state, project, base, cand, tests=("python3 -m pytest -q tests/test_skip.py",)
    )
    assert code == 2 and out["verified"] is False and out["core_status"] != "VERIFIED"


@sandbox
def test_deferred_tests_are_laid_over_the_candidate_and_a_failing_one_refuses(project, state):
    base, test_commit = make_test_commit(project, {"tests/test_regression.py": REGRESSION})
    wrong = candidate(project, base, {"src/calc.py": "def add(a, b):\n    return a * b\n"})
    verifier = " ".join(CHECK[0][1])
    code, out = verify(state, project, base, wrong, "--test-commit", test_commit, tests=(verifier,))
    assert code == 2 and out["verified"] is False and out["core_status"] != "VERIFIED"
    assert out["judged_commit"] != wrong  # the tests were laid over it


@sandbox
def test_deferred_tests_that_pass_on_the_candidate_verify_the_combined_commit(project, state):
    base, test_commit = make_test_commit(project, {"tests/test_regression.py": REGRESSION})
    cand = candidate(project, base, {"src/calc.py": FIX})
    verifier = " ".join(CHECK[0][1])
    code, out = verify(state, project, base, cand, "--test-commit", test_commit, tests=(verifier,))
    assert code == 0, out
    files = git(project, "diff", "--name-only", base, out["judged_commit"]).split()
    assert sorted(files) == ["src/calc.py", "tests/test_regression.py"]
    code, again = verify(
        state, project, base, cand, "--test-commit", test_commit, tests=(verifier,)
    )
    assert code == 2 and again["reason"].startswith("TEST_COMMIT_ALREADY_EXPOSED")


@sandbox
def test_a_tampered_core_response_is_not_verified(project, state, tmp_path):
    def tamper(response):
        hashes = {**response["hashes"], "change_manifest_hash": "sha256:" + "0" * 64}
        return {**response, "hashes": hashes}

    base = base_of(project)
    cand = candidate(project, base, {"src/calc.py": FIX})
    with core_http_server(tmp_path, tamper=tamper) as (url, token_file):
        config = {"verifier_account": False, "core_url": url, "core_token_file": str(token_file)}
        (state / "config.json").write_text(json.dumps(config))
        code, out = verify(state, project, base, cand)
    assert code == 2 and out["verified"] is False
    assert (
        out["reason"] == "CORE_MANIFEST_HASH_MISMATCH"
    )  # Core said VERIFIED; the binding check overrides


def cli(state, *args):
    done = subprocess.run(
        [sys.executable, "-I", "-m", "cairnwork_verify.cli", "--state", str(state), *args],
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    return done.returncode, json.loads(done.stdout)


def test_a_drained_state_refuses_without_touching_anything(project, state):
    (state / "ACCEPTING").unlink()
    base = base_of(project)
    cand = candidate(project, base, {"src/calc.py": FIX})
    code, out = verify(state, project, base, cand)
    assert code == 2 and out["error"] == "VERIFY_NOT_ACCEPTING"
    assert not (state / "runs").exists() and not (state / "verify-runs").exists()


def test_the_state_s_mirror_is_the_only_repository_that_is_written(project, state, tmp_path):
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    git(mirror, "init", "-q")
    config = {"verifier_account": False, "mirror": str(mirror)}
    (state / "config.json").write_text(json.dumps(config))
    base = base_of(project)
    cand = candidate(project, base, {"src/calc.py": FIX, "src/other.py": "x = 1\n"})
    code, out = verify(state, project, base, cand)
    assert code == 2 and out["reason"].startswith("OUT_OF_SCOPE")  # judged in the mirror
    assert git(mirror, "rev-parse", f"refs/cairnwork-bases/{cand}") == cand
    assert git(mirror, "rev-parse", f"refs/cairnwork-bases/{base}") == base
    assert git(project, "for-each-ref", "refs/cairnwork*") == ""  # the caller's repo untouched
    (state / "config.json").write_text(json.dumps({**config, "mirror": str(tmp_path / "nope")}))
    code, out = verify(state, project, base, cand)
    assert code == 2 and out["error"] == "BAD_CONFIG"


@sandbox
def test_a_verified_candidate_is_verified_through_the_mirror(project, state, tmp_path):
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    git(mirror, "init", "-q")
    (state / "config.json").write_text(
        json.dumps({"verifier_account": False, "mirror": str(mirror)})
    )
    base = base_of(project)
    cand = candidate(project, base, {"src/calc.py": FIX})
    code, out = verify(state, project, base, cand)
    assert code == 0, out


def _in_process(project, state, base, cand):
    from cairnwork.intake import Intake
    from cairnwork_verify.verify import verify_candidate

    return verify_candidate(
        Intake(state),
        repo=project,
        base=base,
        candidate=cand,
        allowed_files=("src/calc.py",),
        verifiers=(("v", tuple(VERIFY.split())),),
    )


@sandbox
@pytest.mark.parametrize("readback", ["missing", "unverified"])
def test_notarize_saying_verified_is_not_enough_without_a_bound_readback(
    project, state, monkeypatch, readback
):
    import dataclasses

    from cairnwork.notary import Notary

    real = Notary.verdict

    def forged(self, *args, **kwargs):
        found = real(self, *args, **kwargs)
        if readback == "missing" or found is None:
            return None
        return dataclasses.replace(found, verified=False)

    monkeypatch.setattr(Notary, "verdict", forged)
    base = base_of(project)
    out = _in_process(project, state, base, candidate(project, base, {"src/calc.py": FIX}))
    assert out["verified"] is False and out["status"] == "FAILED"


def test_a_cleanup_failure_does_not_overwrite_the_real_reason(project, state, monkeypatch):
    import cairnwork_verify.verify as module
    from cairnwork.workspace import WorkspaceError

    def broken(*_args):
        raise WorkspaceError("cannot remove")

    monkeypatch.setattr(module, "remove_attempt_clone", broken)
    base = base_of(project)
    cand = candidate(project, base, {"src/calc.py": FIX, "src/other.py": "x = 1\n"})
    out = _in_process(project, state, base, cand)
    assert out["status"] == "REFUSED" and out["reason"].startswith("OUT_OF_SCOPE")
    assert any("cleanup failed" in line for line in out["logs"])


def test_cleanup_removes_a_dead_run_s_leftovers_and_spares_a_live_one(state):
    import fcntl

    runs, markers = state / "runs", state / "verify-runs"
    runs.mkdir()
    markers.mkdir()
    for attempt in ("adead0001", "alive0002"):
        (markers / attempt).touch()
        (runs / f"{attempt}-b-v0.work").mkdir()
        (runs / f"{attempt}.tests.index").touch()
    live = (markers / "alive0002").open("r+")
    fcntl.flock(live, fcntl.LOCK_EX)
    try:
        code, out = cli(state, "cleanup")
    finally:
        live.close()
    assert code == 0 and out == {"cleaned": ["adead0001"]}
    assert not (markers / "adead0001").exists() and not (runs / "adead0001.tests.index").exists()
    assert not (runs / "adead0001-b-v0.work").exists()
    assert (markers / "alive0002").exists() and (runs / "alive0002-b-v0.work").exists()


@sandbox
def test_a_killed_run_is_cleaned_up(project, state):
    import secrets
    import time

    name = f"test_hang_{secrets.token_hex(6)}"
    commit_change(project, {f"tests/{name}.py": f"import time\ndef test_{name}():\n"
                            "    time.sleep(300)\n"})  # fmt: skip
    base = base_of(project)
    cand = candidate(project, base, {"src/calc.py": FIX})
    proc = subprocess.Popen(
        [sys.executable, "-I", "-m", "cairnwork_verify.cli", "--state", str(state), "run",
         "--repo-path", str(project), "--base", base, "--candidate", cand,
         "--allowed-file", "src/calc.py", "--verifier", f"python3 -m pytest -q tests/{name}.py"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip

    def hanging():
        ps = subprocess.run(["ps", "-axo", "command"], capture_output=True, text=True).stdout
        return any(name in line and "pytest" in line for line in ps.splitlines())

    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not hanging():
            time.sleep(0.2)
        assert hanging() and len(list((state / "verify-runs").iterdir())) == 1
    finally:
        proc.kill()  # our own pid only
        proc.wait()
    code, out = cli(state, "cleanup")
    assert code == 0 and len(out["cleaned"]) == 1
    time.sleep(1)
    assert not hanging() and list((state / "verify-runs").iterdir()) == []
    assert not [p for p in (state / "runs").iterdir() if p.name.endswith(".work")]


def test_cleanup_stops_a_dead_run_s_orphan_process(state):
    import os
    import time

    from cairnwork.confined import ATTEMPT_ENV

    (state / "verify-runs").mkdir()
    (state / "verify-runs" / "adead0003").touch()
    orphan = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env={**os.environ, ATTEMPT_ENV: "adead0003"},
    )
    try:
        time.sleep(0.5)
        assert cli(state, "cleanup")[1] == {"cleaned": ["adead0003"]}
        time.sleep(1)
        assert orphan.poll() is not None
    finally:
        orphan.kill()
        orphan.wait()
