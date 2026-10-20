"""Every id or path pattern must match the whole value: "$" alone lets a newline through."""

import pytest
from conftest import git

from cairnwork.gitcmd import OID
from cairnwork.notary import _OID
from cairnwork.pipeline import TaskRejected, TaskSpec, check_task
from cairnwork.verifiers import VerifierRejected, parse_verifier
from cairnwork.workspace import ATTEMPT_ID

GOOD_OID = "a" * 40


@pytest.mark.parametrize(
    ("pattern", "good"),
    [(OID, GOOD_OID), (_OID, GOOD_OID), (ATTEMPT_ID, "a1")],
)
def test_patterns_refuse_a_trailing_newline(pattern, good):
    assert pattern.fullmatch(good)
    assert not pattern.fullmatch(good + "\n")


def test_task_with_a_newline_after_its_base_is_rejected(tmp_path):
    def task(base):
        return TaskSpec(
            idempotency_key="k",
            card_hash="c",
            title="t",
            instructions="i",
            repo=tmp_path,
            base_commit=base,
            allowed_files=("src/a.py",),
            verifiers=(("pytest", ("python3", "-m", "pytest", "tests/test_a.py")),),
        )

    check_task(task(GOOD_OID))
    with pytest.raises(TaskRejected, match="BAD_BASE"):
        check_task(task(GOOD_OID + "\n"))


def test_verifier_target_with_a_newline_is_rejected():
    parse_verifier("pytest", ["python3", "-m", "pytest", "tests/test_a.py"])
    with pytest.raises(VerifierRejected):
        parse_verifier("pytest", ["python3", "-m", "pytest", "tests/test_a.py\n"])


def test_notary_refuses_an_expected_candidate_with_a_newline(tmp_path):
    from cairnwork.notary import Notary, NotaryInputError

    notary = Notary(tmp_path / "notary.sqlite")
    assert notary.verdict("task-1", "a1", expected_candidate=GOOD_OID) is None
    with pytest.raises(NotaryInputError):
        notary.verdict("task-1", "a1", expected_candidate=GOOD_OID + "\n")


def test_attempt_id_with_a_trailing_newline_is_refused(tmp_path):
    from cairnwork.workspace import WorkspaceError, remove_attempt_clone

    with pytest.raises(WorkspaceError, match="must match"):
        remove_attempt_clone(tmp_path, "a1\n")


def test_attempt_base_with_a_trailing_newline_is_refused(repo, tmp_path):
    from cairnwork.workspace import WorkspaceError, create_attempt_clone

    base = git(repo, "rev-parse", "HEAD")
    with pytest.raises(WorkspaceError, match="40-hex"):
        create_attempt_clone(repo, base + "\n", tmp_path / "runs", "a1")
