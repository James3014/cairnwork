"""#77: one path validator for the Issue and submit paths, Cairnwork's own gate files protected,
and check_candidate_commit re-checks protected paths on its own."""

import json
from pathlib import Path

import pytest
from conftest import git

from cairnwork.issue_task import IssueRefused, parse_task_block
from cairnwork.pipeline import TaskRejected, TaskSpec, check_task
from cairnwork.protected_paths import is_protected, path_refusal
from cairnwork.workspace import (
    Attempt,
    CandidateRejected,
    _guarded_refs,
    collect_candidate,
    create_attempt_clone,
)

VERIFY = (("pytest", ("python3", "-m", "pytest", "-q", "tests/test_calc.py")),)
WORKER = ("-c", "user.email=w@example.invalid", "-c", "user.name=w")
INVALID = "TASK_ALLOWED_FILES_INVALID"
PROTECTED = "TASK_ALLOWED_FILES_PROTECTED"

OK = ["src/calc.py", "lib/util.py", "docs/notes.md", "scripts/other.py"]
OWN_GATE = [
    "scripts/check_size.py",
    "scripts/pyright_gate.py",
    "SECURITY.md",
    "ruff.toml",
    "pyrightconfig.json",
    "src/cairnwork/issue_task.py",
    "src/cairnwork/protected_paths.py",
    "src/cairnwork_verify/verify.py",
    "src/cairnwork_sandbox/anything.py",
    "src/cairnwork_protocol/x.py",
    "security.md",
    "src/cairnwork_tools/gate.py",
    "src/CairnWork_new/x.py",
    "scripts/setup_verifier_user.sh",
    "scripts/setup_verifier_python.sh",
    "scripts/setup_target_venv.sh",
]
DOTTED = [".env", "src/.env", ".ssh/config", "a/.hidden/b.py", ".git/config"]
MALFORMED = ["", "/etc/passwd", "../x.py", "a//b.py", "a/./b.py", "a/", "a\\b.py", "a b.py", "x\n"]
PROTECTED_NOW = [
    "AGENTS.md",
    "pyproject.toml",
    "tests/conftest.py",
    ".github/workflows/x.yml",
]


def refusal_code(path) -> str | None:
    refusal = path_refusal(path)
    return refusal[0] if refusal else None


def issue_verdict(path: str) -> str | None:
    block = json.dumps(
        {
            "difficulty": "easy",
            "allowed_files": [path],
            "verification": ["python3 -m pytest tests/test_calc.py"],
            "capability_ids": ["c1"],
        }
    )
    try:
        parse_task_block(f"```eia-task\n{block}\n```")
    except IssueRefused as exc:
        return exc.code
    return None


def submit_verdict(path: str) -> str | None:
    task = TaskSpec("k", "card", "t", "d", Path("."), "a" * 40, (path,), VERIFY)
    try:
        check_task(task)
    except TaskRejected as exc:
        return exc.code
    return None


@pytest.mark.parametrize("path", OK)
def test_ordinary_paths_pass_every_entry_point(path):
    assert path_refusal(path) is None
    assert issue_verdict(path) is None and submit_verdict(path) is None


@pytest.mark.parametrize("path", OWN_GATE)
def test_cairnworks_own_gate_files_are_protected(path):
    assert is_protected(path)
    assert refusal_code(path) == PROTECTED
    assert issue_verdict(path) == PROTECTED
    assert submit_verdict(path) == PROTECTED


@pytest.mark.parametrize("path", DOTTED + MALFORMED)
def test_dotted_and_malformed_paths_are_invalid_on_both_paths(path):
    # the submit path used to accept ".env"; both now share one validator
    assert refusal_code(path) == INVALID
    assert issue_verdict(path) == INVALID
    assert submit_verdict(path) == "BAD_SCOPE"


@pytest.mark.parametrize("path", OK + OWN_GATE + DOTTED + MALFORMED + PROTECTED_NOW)
def test_same_input_same_verdict_on_issue_and_submit_paths(path):
    issue, submit = issue_verdict(path), submit_verdict(path)
    assert (issue is None) == (submit is None), (path, issue, submit)
    if issue:
        # submit spells "invalid" BAD_SCOPE and may report a protected verifier input first
        same = {INVALID: "BAD_SCOPE"}.get(issue, issue)
        assert submit in (same, "PROTECTED_VERIFIER_INPUT"), (path, issue, submit)


def test_non_string_path_is_invalid():
    assert refusal_code(None) == INVALID and refusal_code(7) == INVALID


# --- check_candidate_commit: defense in depth -------------------------------------------


def _attempt(repo, tmp_path):
    base = git(repo, "rev-parse", "HEAD")
    made = create_attempt_clone(repo, base, tmp_path / "attempts", "a1")
    return Attempt("a1", base, made.path, _guarded_refs(repo, "a1")), made.path


def _commit(clone):
    git(clone, "add", "-A")
    git(clone, *WORKER, "commit", "-q", "-m", "candidate")
    return git(clone, "rev-parse", "HEAD")


def test_candidate_check_refuses_a_protected_path_even_when_allowed(repo, tmp_path):
    attempt, clone = _attempt(repo, tmp_path)
    (clone / "scripts").mkdir(exist_ok=True)
    (clone / "scripts" / "check_size.py").write_text("LIMIT = 10**9\n")
    _commit(clone)
    # a future entry point that skips check_task passes the protected file as allowed
    with pytest.raises(CandidateRejected) as info:
        collect_candidate(repo, attempt, ("scripts/check_size.py",))
    assert info.value.code == "PROTECTED_PATH"


def test_candidate_check_reports_scope_before_protection(repo, tmp_path):
    attempt, clone = _attempt(repo, tmp_path)
    (clone / "SECURITY.md").write_text("nothing to see\n")
    _commit(clone)
    with pytest.raises(CandidateRejected) as info:
        collect_candidate(repo, attempt, ("src/a.py",))
    assert info.value.code == "OUT_OF_SCOPE"


def test_write_through_a_tracked_symlink_is_out_of_scope(repo, tmp_path):
    (repo / "src" / "link.py").symlink_to("a.py")  # tracked symlink, target src/a.py
    git(repo, "add", "-A")
    git(repo, *WORKER, "commit", "-q", "-m", "symlink")
    attempt, clone = _attempt(repo, tmp_path)
    (clone / "src" / "link.py").write_text("x = 99\n")  # follows the link: changes a.py
    _commit(clone)
    with pytest.raises(CandidateRejected) as info:
        collect_candidate(repo, attempt, ("src/link.py",))
    assert info.value.code == "OUT_OF_SCOPE" and info.value.args[0] if False else True
    assert info.value.code == "OUT_OF_SCOPE"
    # naming the target as the allowed file is what lets the change in
    assert collect_candidate(repo, attempt, ("src/a.py",)).paths == ("src/a.py",)


@pytest.mark.parametrize("entry", [["a.py"], 7, None, {"a": 1}, ("a.py",)])
def test_malformed_allowed_files_entries_are_refused_not_a_crash(entry):
    task = TaskSpec("k", "card", "t", "d", Path("."), "a" * 40, (entry,), VERIFY)  # type: ignore[arg-type]
    with pytest.raises(TaskRejected) as info:
        check_task(task)
    assert info.value.code == "BAD_SCOPE"


def test_other_scripts_stay_editable():
    assert path_refusal("scripts/build.sh") is None and path_refusal("scripts/x.py") is None


def test_candidate_check_refuses_a_new_src_cairnwork_package(repo, tmp_path):
    attempt, clone = _attempt(repo, tmp_path)
    (clone / "src" / "cairnwork_tools").mkdir()
    (clone / "src" / "cairnwork_tools" / "gate.py").write_text("x = 1\n")
    _commit(clone)
    with pytest.raises(CandidateRejected) as info:
        collect_candidate(repo, attempt, ("src/cairnwork_tools/gate.py",))
    assert info.value.code == "PROTECTED_PATH"
