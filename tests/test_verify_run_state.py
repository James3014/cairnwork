"""``cairnwork-verify``: drain, mirror, scratch clones, markers and crash cleanup."""

import json
import subprocess
import sys

import pytest
from conftest import commit_change, git
from test_deferred_tests import CHECK, REGRESSION, make_test_commit
from verify_helpers import FIX, base_of, candidate, cli, has_object, in_process, make_mirror, verify

sandbox = pytest.mark.sandbox


def test_a_drained_state_refuses_without_touching_anything(project, state):
    (state / "ACCEPTING").unlink()
    base = base_of(project)
    cand = candidate(project, base, {"src/calc.py": FIX})
    code, out = verify(state, project, base, cand)
    assert code == 2 and out["error"] == "VERIFY_NOT_ACCEPTING"
    assert not (state / "runs").exists() and not (state / "verify-runs").exists()


def test_the_shared_mirror_never_receives_the_external_candidate(project, state, tmp_path):
    mirror = make_mirror(project, tmp_path)
    config = {"verifier_account": False, "mirror": str(mirror)}
    (state / "config.json").write_text(json.dumps(config))
    base = base_of(project)
    cand = candidate(project, base, {"src/calc.py": FIX, "src/other.py": "x = 1\n"})
    refs_before = git(mirror, "for-each-ref")
    code, out = verify(state, project, base, cand)
    assert code == 2 and out["reason"].startswith("OUT_OF_SCOPE")  # judged in a scratch clone
    assert git(mirror, "for-each-ref") == refs_before
    assert not has_object(mirror, cand) and has_object(project, cand)
    assert git(project, "for-each-ref", "refs/cairnwork*") == ""
    assert not list((state / "verify-scratch").iterdir())  # the scratch clone is gone
    (state / "config.json").write_text(json.dumps({**config, "mirror": str(tmp_path / "nope")}))
    code, out = verify(state, project, base, cand)
    assert code == 2 and out["error"] == "BAD_CONFIG"


@sandbox
def test_a_verified_candidate_is_verified_through_the_mirror(project, state, tmp_path):
    mirror = make_mirror(project, tmp_path)
    (state / "config.json").write_text(
        json.dumps({"verifier_account": False, "mirror": str(mirror)})
    )
    base = base_of(project)
    cand = candidate(project, base, {"src/calc.py": FIX})
    code, out = verify(state, project, base, cand)
    assert code == 0, out
    assert not has_object(mirror, cand)


@sandbox
def test_a_deferred_test_commit_verifies_through_the_mirror(project, state, tmp_path):
    base, test_commit = make_test_commit(project, {"tests/test_regression.py": REGRESSION})
    mirror = make_mirror(project, tmp_path)
    (state / "config.json").write_text(
        json.dumps({"verifier_account": False, "mirror": str(mirror)})
    )
    cand = candidate(project, base, {"src/calc.py": FIX})
    refs_before = git(mirror, "for-each-ref")
    verifier = " ".join(CHECK[0][1])
    code, out = verify(state, project, base, cand, "--test-commit", test_commit, tests=(verifier,))
    assert code == 0, out
    assert git(mirror, "for-each-ref") == refs_before
    assert not has_object(mirror, cand) and not has_object(mirror, test_commit)
    assert not has_object(mirror, out["judged_commit"])


@sandbox
def test_the_exposure_check_looks_in_the_mirror_as_the_worker_path_does(project, state, tmp_path):
    base, test_commit = make_test_commit(project, {"tests/test_regression.py": REGRESSION})
    mirror = make_mirror(project, tmp_path)
    git(mirror, "fetch", "-q", "origin", test_commit)  # the mirror has seen the tests
    (state / "config.json").write_text(
        json.dumps({"verifier_account": False, "mirror": str(mirror)})
    )
    cand = candidate(project, base, {"src/calc.py": FIX})
    verifier = " ".join(CHECK[0][1])
    code, out = verify(state, project, base, cand, "--test-commit", test_commit, tests=(verifier,))
    assert code == 2 and out["reason"].startswith("TEST_COMMIT_ALREADY_EXPOSED")
    assert not has_object(project, test_commit)


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
    out = in_process(project, state, base, candidate(project, base, {"src/calc.py": FIX}))
    assert out["verified"] is False and out["status"] == "FAILED"


def test_a_cleanup_failure_does_not_overwrite_the_real_reason(project, state, monkeypatch):
    import cairnwork_verify.verify as module
    from cairnwork.workspace import WorkspaceError

    def broken(*_args):
        raise WorkspaceError("cannot remove")

    monkeypatch.setattr(module, "remove_attempt_clone", broken)
    base = base_of(project)
    cand = candidate(project, base, {"src/calc.py": FIX, "src/other.py": "x = 1\n"})
    out = in_process(project, state, base, cand)
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
        (state / "verify-scratch" / attempt).mkdir(parents=True)
    live = (markers / "alive0002").open("r+")
    fcntl.flock(live, fcntl.LOCK_EX)
    try:
        code, out = cli(state, "cleanup")
    finally:
        live.close()
    assert code == 0 and out == {"cleaned": ["adead0001"]}
    assert not (markers / "adead0001").exists() and not (runs / "adead0001.tests.index").exists()
    assert not (runs / "adead0001-b-v0.work").exists()
    assert not (state / "verify-scratch" / "adead0001").exists()
    assert (state / "verify-scratch" / "alive0002").exists()
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
        assert cli(state, "cleanup")[1] == {"cleaned": []}  # the run is alive: hands off
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


def test_a_drain_between_the_first_check_and_the_run_lock_is_honoured(project, state, monkeypatch):
    import cairnwork_verify.verify as module
    from cairnwork.intake import IntakeRefused

    real = module.runlock.held

    def drain_first(*args, **kwargs):
        (state / "ACCEPTING").unlink()  # the operator drains while we wait for the lock
        return real(*args, **kwargs)

    monkeypatch.setattr(module.runlock, "held", drain_first)
    base = base_of(project)
    cand = candidate(project, base, {"src/calc.py": FIX})
    with pytest.raises(IntakeRefused) as refused:
        in_process(project, state, base, cand)
    assert refused.value.code == "VERIFY_NOT_ACCEPTING"
    assert not (state / "verify-runs").exists() or not list((state / "verify-runs").iterdir())
    assert not (state / "verify-scratch").exists()


def test_a_marker_removed_between_open_and_lock_is_recreated(state, monkeypatch):
    import os

    import cairnwork_verify.verify as module
    from cairnwork.intake import Intake

    intake, calls = Intake(state), []

    def cleaner_strikes():  # a cleaner holding the lock deletes the file we just opened
        if not calls:
            (state / "verify-runs" / "a1").unlink()
        calls.append(1)

    monkeypatch.setattr(module, "_gap", cleaner_strikes)
    with module._marked(intake, "a1"):
        held = os.stat(state / "verify-runs" / "a1")  # the path names the file we hold
        assert held.st_nlink == 1 and len(calls) == 2
    assert not (state / "verify-runs" / "a1").exists()


def test_cleanup_leaves_a_marker_that_was_replaced_while_it_held_the_lock(state, monkeypatch):
    import cairnwork_verify.verify as module
    from cairnwork.intake import Intake

    markers = state / "verify-runs"
    markers.mkdir()
    (markers / "adead0004").touch()
    (state / "runs").mkdir()
    (state / "runs" / "adead0004.tests.index").touch()

    def new_run_starts():  # a run recreated the marker after we opened the old one
        (markers / "adead0004").unlink()
        (markers / "adead0004").touch()

    monkeypatch.setattr(module, "_gap", new_run_starts)
    assert module.cleanup(Intake(state)) == {"cleaned": []}
    assert (markers / "adead0004").exists()
    assert (state / "runs" / "adead0004.tests.index").exists()


def test_cleanup_tolerates_a_marker_that_vanishes_before_it_is_opened(state, monkeypatch):
    import os

    import cairnwork_verify.verify as module
    from cairnwork.intake import Intake

    markers = state / "verify-runs"
    markers.mkdir()
    (markers / "agone0005").touch()
    real = os.open

    def vanishing(path, *args, **kwargs):
        if str(path).endswith("agone0005"):
            os.unlink(path)  # the run finished between the listing and the open
        return real(path, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", vanishing)
    assert module.cleanup(Intake(state)) == {"cleaned": []}


def test_run_cleans_up_dead_runs_first(project, state):
    (state / "verify-runs").mkdir()
    (state / "verify-runs" / "adead0006").touch()
    (state / "runs").mkdir()
    (state / "runs" / "adead0006.tests.index").touch()
    base = base_of(project)
    cand = candidate(project, base, {"src/calc.py": FIX, "src/other.py": "x = 1\n"})
    code, out = verify(state, project, base, cand)
    assert code == 2 and out["reason"].startswith("OUT_OF_SCOPE")
    assert not (state / "verify-runs" / "adead0006").exists()
    assert not (state / "runs" / "adead0006.tests.index").exists()


@sandbox
def test_a_candidate_built_on_a_hidden_test_commit_cannot_burn_it(project, state, tmp_path):
    from cairnwork.deferred_tests import never_seen

    base, test_commit = make_test_commit(project, {"tests/test_regression.py": REGRESSION})
    mirror = make_mirror(project, tmp_path)
    (state / "config.json").write_text(
        json.dumps({"verifier_account": False, "mirror": str(mirror)})
    )
    git(project, "fetch", "-q", "origin", test_commit)  # the attacker's repo holds the tests
    git(project, "checkout", "-q", "--detach", test_commit)
    on_tests = commit_change(project, {"src/calc.py": FIX})
    git(project, "branch", "evil", on_tests)
    git(project, "checkout", "-q", "main")
    verifier = " ".join(CHECK[0][1])
    code, out = verify(
        state, project, base, on_tests, "--test-commit", test_commit, tests=(verifier,)
    )
    assert code == 2 and out["reason"].startswith("NOT_ONE_COMMIT_ON_BASE")
    assert not has_object(mirror, test_commit) and not has_object(mirror, on_tests)
    never_seen(mirror, test_commit)  # still passes: nothing was burned


def test_the_parent_is_checked_in_the_callers_repository_before_any_clone(
    project, state, monkeypatch
):
    import cairnwork_verify.verify as module

    def no_clone(*_args):
        raise AssertionError("a scratch clone was made for a candidate that is not on the base")

    monkeypatch.setattr(module, "_scratch", no_clone)
    base = base_of(project)
    first = candidate(project, base, {"src/calc.py": FIX})
    git(project, "checkout", "-q", "--detach", first)
    second = commit_change(project, {"src/calc.py": FIX + "\n"})
    git(project, "branch", "two", second)
    git(project, "checkout", "-q", "main")
    out = in_process(project, state, base, second)
    assert out["status"] == "REFUSED" and out["reason"].startswith("NOT_ONE_COMMIT_ON_BASE")
