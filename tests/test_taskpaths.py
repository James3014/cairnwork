"""PRESERVED #4: path authenticity against the base tree, before dispatch."""

import io
import json
import os

import pytest
from conftest import git

import cairnwork.intake as intake_module
import cairnwork.route as route_module
from cairnwork.intake import Intake
from cairnwork.pipeline import TaskRejected
from cairnwork.taskpaths import check_task_paths


@pytest.fixture
def base(repo):
    (repo / "tests").mkdir()
    (repo / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")
    (repo / "src" / "run.sh").write_text("#!/bin/sh\n")
    (repo / "src" / "run.sh").chmod(0o755)
    (repo / "src" / "link.py").symlink_to("a.py")
    (repo / "plain").write_text("not a directory\n")
    (repo / "linkdir").symlink_to("src")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "paths")
    return repo, git(repo, "rev-parse", "HEAD")


def refusal(base, files, targets=(), tests=()):
    repo, commit = base
    with pytest.raises(TaskRejected) as caught:
        check_task_paths(repo, commit, tuple(files), tuple(targets), tuple(tests))
    return caught.value.code


def test_a_symlink_as_an_allowed_file_is_refused(base):
    assert refusal(base, ["src/link.py"]) == "TASK_PATH_NOT_REGULAR_FILE"


def test_a_directory_as_an_allowed_file_is_refused(base):
    assert refusal(base, ["src"]) == "TASK_PATH_NOT_REGULAR_FILE"


def test_a_file_or_a_symlink_as_a_parent_is_refused(base):
    assert refusal(base, ["plain/y.py"]) == "TASK_PATH_PARENT_INVALID"
    assert refusal(base, ["linkdir/a.py"]) == "TASK_PATH_PARENT_INVALID"
    assert refusal(base, ["src/a.py/y.py"]) == "TASK_PATH_PARENT_INVALID"


def test_a_missing_parent_directory_is_refused(base):
    assert refusal(base, ["src/new_dir_missing/y.py"]) == "TASK_PATH_PARENT_INVALID"
    assert refusal(base, ["nope/deeper/y.py"]) == "TASK_PATH_PARENT_INVALID"


def test_regular_files_and_a_new_file_in_an_existing_directory_pass(base):
    repo, commit = base
    check_task_paths(repo, commit, ("src/a.py", "src/run.sh", "src/new.py", "top.py"), ())


def test_an_unreadable_base_is_refused(base):
    repo, _ = base
    with pytest.raises(TaskRejected) as caught:
        check_task_paths(repo, "0" * 40, ("src/a.py",), ())
    assert caught.value.code == "TASK_PATH_UNREADABLE"


def test_a_missing_verifier_target_needs_an_allowed_file_or_the_test_commit(base):
    target = "tests/test_new.py::test_x"
    assert refusal(base, ["src/a.py"], [target]) == "TASK_VERIFIER_TARGET_MISSING"
    repo, commit = base
    check_task_paths(repo, commit, ("src/a.py",), (target,), ("tests/test_new.py",))
    check_task_paths(repo, commit, ("tests/test_new.py",), (target,))


def test_an_existing_verifier_target_passes_and_a_symlinked_one_is_refused(base):
    repo, commit = base
    check_task_paths(repo, commit, ("src/a.py",), ("tests/test_a.py::test_a",))
    assert refusal(base, ["src/a.py"], ["src/link.py"]) == "TASK_PATH_NOT_REGULAR_FILE"


def payload(repo, **changes):
    return {
        "idempotency_key": "k1",
        "card_hash": "card-k1",
        "title": "fix",
        "instructions": "fix it",
        "repo": str(repo),
        "base_commit": git(repo, "rev-parse", "HEAD"),
        "allowed_files": ["src/a.py"],
        "verifiers": [["pytest", ["python3", "-m", "pytest", "tests/test_a.py"]]],
    } | changes


@pytest.fixture
def started(monkeypatch):
    calls = []
    monkeypatch.setattr(intake_module, "_spawn", lambda *a, **k: calls.append(a))
    return calls


def test_submit_refuses_before_any_worker_starts(tmp_path, base, started):
    repo, _ = base
    intake = Intake(tmp_path / "state")
    intake.set_accepting(True)
    for files, code in (
        (["src/link.py"], "TASK_PATH_NOT_REGULAR_FILE"),
        (["src/new_dir_missing/y.py"], "TASK_PATH_PARENT_INVALID"),
    ):
        with pytest.raises(TaskRejected) as caught:
            intake.submit(payload(repo, allowed_files=files))
        assert caught.value.code == code
    verifier = [["pytest", ["python3", "-m", "pytest", "tests/test_gone.py"]]]
    with pytest.raises(TaskRejected) as caught:
        intake.submit(payload(repo, verifiers=verifier))
    assert caught.value.code == "TASK_VERIFIER_TARGET_MISSING"
    assert started == [] and not list((intake.state / "requests").iterdir())
    assert intake.submit(payload(repo))["status"] == "BOUND" and len(started) == 1


def test_a_task_with_a_test_commit_is_not_compared_with_it(tmp_path, base, started):
    repo, _ = base
    intake = Intake(tmp_path / "state")
    intake.set_accepting(True)
    verifier = [["pytest", ["python3", "-m", "pytest", "tests/test_hidden.py"]]]
    hidden = {"verifiers": verifier, "test_commit": "1" * 40}
    assert intake.submit(payload(repo, **hidden))["status"] == "BOUND"


def test_shadow_check_rehearses_the_refusal(tmp_path, base, monkeypatch, capsys):
    repo, _ = base
    state = tmp_path / "state"
    state.mkdir(mode=0o700)

    def check(**changes):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload(repo, **changes))))
        code = route_module.main(["--state", str(state), "check"])
        return code, json.loads(capsys.readouterr().out)

    assert check() == (0, {"would_accept": False})
    code, out = check(allowed_files=["src/link.py"])
    assert (code, out["error"]) == (2, "TASK_PATH_NOT_REGULAR_FILE")
    code, out = check(allowed_files=["src/new_dir_missing/y.py"])
    assert (code, out["error"]) == (2, "TASK_PATH_PARENT_INVALID")
    assert sorted(os.listdir(state)) == []
