"""#82: operator-configured ``import_paths`` let a src-layout project import its own package.

A project laid out as ``src/<pkg>/`` without a pytest ``pythonpath`` config cannot import its own
package from a plain checkout (the target venv does not install the project). An operator sets
``import_paths: ["src"]`` in the state config; it is prepended to every verifier run's import path.
"""

import json

import pytest
from conftest import commit_change

from cairnwork.intake import Intake, IntakeRefused
from cairnwork.issue_task import check_import_paths
from cairnwork.verifiers import parse_verifier, run_verifiers

SRC_LAYOUT = {
    "src/mypkg/__init__.py": "",
    "src/mypkg/calc.py": "def add(a, b):\n    return a + b\n",
    "tests/test_calc.py": (
        "from mypkg.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    ),
}


def test_check_import_paths_accepts_clean_relative_dirs():
    assert check_import_paths([]) == ()
    assert check_import_paths(["src", "lib/pkg"]) == ("src", "lib/pkg")


@pytest.mark.parametrize(
    "bad",
    [
        "src",  # not a list
        [".."],  # escapes the checkout
        ["src/../x"],  # dot component
        ["/abs"],  # absolute
        ["a*"],  # glob
        ["-x"],  # leading dash
        ["src", "src"],  # duplicate
        [f"d{i}" for i in range(9)],  # more than 8
        [1],  # not a string
    ],
)
def test_check_import_paths_rejects_bad(bad):
    with pytest.raises(ValueError):
        check_import_paths(bad)


def test_import_paths_config_is_validated(tmp_path):
    intake = Intake(tmp_path / "state")
    intake.prepare()
    (intake.state / "config.json").write_text(json.dumps({"import_paths": ["src"]}))
    assert intake.config()["import_paths"] == ["src"]
    (intake.state / "config.json").write_text(json.dumps({"import_paths": ["../escape"]}))
    with pytest.raises(IntakeRefused, match="BAD_CONFIG"):
        intake.config()


def test_import_paths_default_is_empty(tmp_path):
    intake = Intake(tmp_path / "state")
    intake.prepare()
    assert intake.config()["import_paths"] == []


@pytest.mark.sandbox
def test_src_layout_project_verifies_only_with_import_paths(repo, tmp_path):
    base = commit_change(repo, SRC_LAYOUT)
    spec = parse_verifier("pytest", ["python3", "-m", "pytest", "tests/test_calc.py"])

    def verify(run_id, import_paths):
        return run_verifiers(
            repo,
            base,
            (spec,),
            tmp_path / "runs",
            run_id,
            base_commit=base,
            import_paths=import_paths,
        )[0]

    without = verify("r1", ())
    assert (
        not without.observation.passed
    )  # src/ is not on the import path: mypkg cannot be imported
    with_src = verify("r2", ("src",))
    assert with_src.observation.passed, with_src.log_path.read_text()
