"""A root module is writable only when the base tracks it and its name is safe."""

from pathlib import Path

import pytest
from conftest import commit_change, git

from cairnwork.pipeline import TaskRejected, TaskSpec, check_task
from cairnwork.verifier_spec import protected_verifier_inputs

VERIFY = (("pytest", ("python3", "-m", "pytest", "-q", "tests/test_calc.py")),)


def task(repo: Path, path: str) -> TaskSpec:
    return TaskSpec(
        "key", "card", "title", "fix", repo, git(repo, "rev-parse", "HEAD"), (path,), VERIFY
    )


def test_existing_root_module_is_allowed(repo):
    commit_change(repo, {"calc.py": "def add(a, b):\n    return a + b\n"})
    assert check_task(task(repo, "calc.py"))


@pytest.mark.parametrize("name", ("json.py", "pytest.py", "cairnwork.py"))
def test_shadowing_root_module_is_refused_even_if_tracked(repo, name):
    commit_change(repo, {name: "x = 1\n"})
    with pytest.raises(TaskRejected, match="PROTECTED_VERIFIER_INPUT"):
        check_task(task(repo, name))


def test_new_root_module_and_pth_are_refused(repo):
    for name in ("new_module.py", "evil.pth"):
        with pytest.raises(TaskRejected, match="PROTECTED_VERIFIER_INPUT"):
            check_task(task(repo, name))


def test_symlink_root_module_is_refused(repo):
    (repo / "calc.py").symlink_to("src/a.py")
    git(repo, "add", "calc.py")
    git(repo, "commit", "-q", "-m", "symlink")
    with pytest.raises(TaskRejected, match="PROTECTED_VERIFIER_INPUT"):
        check_task(task(repo, "calc.py"))


def test_missing_module_inventory_fails_closed(repo, monkeypatch):
    commit_change(repo, {"calc.py": "x = 1\n"})

    def unavailable(_python):
        raise OSError("interpreter unavailable")

    monkeypatch.setattr("cairnwork.verifier_spec._importable_names", unavailable)
    with pytest.raises(TaskRejected, match="PROTECTED_VERIFIER_INPUT"):
        check_task(task(repo, "calc.py"))


def test_configured_verifier_inventory_decides_shadowing(repo, monkeypatch):
    commit_change(repo, {"calc.py": "x = 1\n"})
    seen = []

    def inventory(python):
        seen.append(python)
        return {"calc"}

    monkeypatch.setattr("cairnwork.verifier_spec._importable_names", inventory)
    with pytest.raises(TaskRejected, match="PROTECTED_VERIFIER_INPUT"):
        check_task(task(repo, "calc.py"), verifier_python="/verifier/python")
    assert seen == ["/verifier/python"]


def test_root_module_without_base_evidence_stays_protected():
    assert protected_verifier_inputs(("calc.py",)) == ("calc.py",)
