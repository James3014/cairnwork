"""pytest settings come from the base commit, through an allow-list (pytest_config.py)."""

import configparser
import json
import os
import sys

import pytest
from conftest import commit_change, git

import cairnwork.pipeline as pipeline
import cairnwork.pipeline_judge as pipeline_judge
from cairnwork import pytest_config
from cairnwork.intake import Intake, IntakeRefused
from cairnwork.journal import Journal
from cairnwork.notary import Notary
from cairnwork.ownership import OwnershipLedger
from cairnwork.pytest_config import PytestConfigError, PytestSettings, base_settings
from cairnwork.verifier_account import VerifierAccount, VerifierAccountError
from cairnwork.verifiers import parse_verifier, run_verifiers

BASE_INI = """[pytest]
python_functions = check_*
strict_xfail = true
markers =
    slow: a slow test
pythonpath = lib ../outside /abs
addopts = -p evil --rootdir=/ --import-mode=importlib -x --strict-markers
cache_dir = /tmp/x
required_plugins = evil
"""


def read_ini(text: bytes) -> dict[str, str]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(text.decode())
    return dict(parser.items("pytest"))


@pytest.fixture
def based(repo):
    """A base commit with a pytest.ini; returns (repo, base sha)."""
    return repo, commit_change(repo, {"pytest.ini": BASE_INI, "tests/test_c.py": TEST_C})


TEST_C = (
    "def check_one():\n    assert True\n"
    "def check_import_mode(pytestconfig):\n"
    "    assert pytestconfig.getoption('importmode') == 'importlib'\n"
)


def test_only_allow_listed_keys_are_copied(based, tmp_path):
    repo, base = based
    settings = base_settings(repo, base)
    ini = read_ini(settings.ini_text(tmp_path / "co"))
    assert set(ini) == {"python_functions", "strict_xfail", "markers", "pythonpath"}
    assert ini["python_functions"] == "check_*"
    assert ini["pythonpath"].split() == [str(tmp_path / "co"), str(tmp_path / "co" / "lib")]
    assert settings.args() == ["--import-mode=importlib", "--strict-markers"]
    assert {"addopts", "cache_dir", "required_plugins", "pythonpath"} <= set(settings.dropped)


@pytest.mark.parametrize(
    ("addopts", "mode"),
    [
        ("--import-mode=append", "append"),
        ("--import-mode importlib -p x", "importlib"),
        ("--import-mode=evil", None),
        ("--import-mode", None),
        ("'unclosed --import-mode=append", None),
    ],
)
def test_only_a_known_import_mode_is_kept_from_addopts(repo, addopts, mode):
    base = commit_change(repo, {"pytest.ini": f"[pytest]\naddopts = {addopts}\n"})
    assert base_settings(repo, base).import_mode == mode


@pytest.mark.parametrize(
    ("addopts", "kept"),
    [
        ("-W error -p x", ("-W", "error")),
        ("-Werror::UserWarning --strict-config", ("-W", "error::UserWarning", "--strict-config")),
        ("--strict-markers --tb=no -x --rootdir=/ -W", ("--strict-markers",)),
        ("-c other.ini --confcutdir=/ -o addopts=", ()),
    ],
)
def test_only_strictness_options_are_kept_from_addopts(repo, addopts, kept):
    base = commit_change(repo, {"pytest.ini": f"[pytest]\naddopts = {addopts}\n"})
    assert base_settings(repo, base).strict_args == kept


def test_plugin_keys_need_the_plugin(repo, tmp_path):
    base = commit_change(repo, {"pytest.ini": "[pytest]\nasyncio_mode = auto\ntimeout = 30\n"})
    settings = base_settings(repo, base)
    plugins = ("pytest_asyncio.plugin", "pytest_timeout")
    ini = read_ini(settings.ini_text(tmp_path, plugins))
    assert ini["asyncio_mode"] == "auto" and ini["timeout"] == "30"
    assert settings.args(plugins) == ["-p", "pytest_asyncio.plugin", "-p", "pytest_timeout"]
    with pytest.raises(PytestConfigError, match="sets timeout, but pytest_timeout"):
        settings.ini_text(tmp_path, ("pytest_asyncio.plugin",))
    with pytest.raises(PytestConfigError, match="sets asyncio_mode, but pytest_asyncio"):
        settings.ini_text(tmp_path, ("pytest_timeout",))


def test_candidate_config_changes_are_ignored(based):
    repo, base = based
    commit_change(
        repo,
        {
            "pytest.ini": "[pytest]\naddopts = -p evil\npython_functions = nothing_*\n",
            "pyproject.toml": '[tool.pytest.ini_options]\npython_functions = ["x_*"]\n',
        },
    )
    assert dict(base_settings(repo, base).values)["python_functions"] == "check_*"
    assert dict(base_settings(repo, "HEAD").values)["python_functions"] == "nothing_*"


def test_pyproject_is_read_when_there_is_no_pytest_ini(repo, tmp_path):
    base = commit_change(
        repo,
        {
            "pyproject.toml": (
                "[tool.pytest.ini_options]\n"
                'markers = ["a: one", "b: two"]\n'
                "xfail_strict = true\n"
                'asyncio_mode = "auto\\naddopts = -p evil\\n[x]"\n'
            )
        },
    )
    settings = base_settings(repo, base)
    assert settings.source == "pyproject.toml"
    text = settings.ini_text(tmp_path, ("pytest_asyncio.plugin",))
    ini = read_ini(text)
    assert set(ini) == {"markers", "xfail_strict", "asyncio_mode", "pythonpath"}
    assert ini["xfail_strict"] == "true" and "addopts" in ini["asyncio_mode"]  # continued
    assert b"\naddopts" not in text and b"\n[x]" not in text


def test_pytest_ini_wins_over_pyproject_and_absent_config_is_empty(repo):
    assert base_settings(repo, git(repo, "rev-parse", "HEAD")) == PytestSettings()
    assert base_settings(repo, None) == PytestSettings()
    base = commit_change(
        repo,
        {"pytest.ini": "[other]\n", "pyproject.toml": "[tool.pytest.ini_options]\nx = 1\n"},
    )
    assert base_settings(repo, base) == PytestSettings(source="pytest.ini")


def test_a_symlink_or_broken_base_config_fails(repo):
    os.symlink("/etc/hosts", repo / "pytest.ini")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "link")
    with pytest.raises(PytestConfigError, match="not a regular file"):
        base_settings(repo, git(repo, "rev-parse", "HEAD"))
    git(repo, "rm", "-q", "pytest.ini")
    base = commit_change(repo, {"pytest.ini": "[pytest]\na = 1\na = 2\n"})
    with pytest.raises(PytestConfigError, match="cannot be parsed"):
        base_settings(repo, base)


def test_installed_plugins_are_found_not_imported():
    found = pytest_config.installed_plugins(sys.executable)
    assert set(found) <= {module for _, module, _ in pytest_config.PLUGINS}


@pytest.mark.sandbox
def test_a_run_uses_base_settings_not_the_candidates(based, tmp_path):
    """check_one is a test only under the base's python_functions."""
    repo, base = based
    candidate = commit_change(repo, {"pytest.ini": "[pytest]\npython_functions = nope_*\n"})
    spec = parse_verifier("pytest", ["python3", "-m", "pytest", "tests/test_c.py"])

    def verify(base_commit, run_id):
        return run_verifiers(
            repo, candidate, (spec,), tmp_path / "runs", run_id, base_commit=base_commit
        )[0]

    good = verify(base, "r1")
    assert good.observation.passed, good.log_path.read_text()
    assert not verify(None, "r2").observation.passed  # no settings: nothing collected


def test_the_runner_passes_python_and_base_commit(monkeypatch, repo, tmp_path):
    seen = {}

    def fake_run_verifiers(*args, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop here")

    # run_verifiers is called from pipeline_judge.judge_candidate (split out of pipeline.py)
    monkeypatch.setattr(pipeline_judge, "run_verifiers", fake_run_verifiers)

    class Worker:
        def run(self, workdir, _task):
            (workdir / "src" / "a.py").write_text("x = 2\n")

    task = pipeline.TaskSpec(
        "k",
        "card-k",
        "t",
        "i",
        repo,
        git(repo, "rev-parse", "HEAD"),
        ("src/a.py",),
        (("pytest", ("python3", "-m", "pytest", "tests/test_a.py")),),
    )
    result = pipeline.run_task(
        task,
        ledger=OwnershipLedger(tmp_path / "o.sqlite"),
        notary=Notary(tmp_path / "n.sqlite"),
        journal=Journal(tmp_path / "j.sqlite"),
        worker=Worker(),
        run_root=tmp_path / "runs",
        verifier_python="/opt/target/bin/python",
    )
    assert result.status == "FAILED" and "stop here" in result.reason
    assert seen["python"] == "/opt/target/bin/python"
    assert seen["base_commit"] == task.base_commit


@pytest.mark.parametrize("value", ["python", 1, ["/x"]])
def test_verifier_python_must_be_absolute(tmp_path, value):
    intake = Intake(tmp_path / "state")
    intake.prepare()
    (intake.state / "config.json").write_text(json.dumps({"verifier_python": value}))
    with pytest.raises(IntakeRefused, match="BAD_CONFIG"):
        intake.config()
    (intake.state / "config.json").write_text('{"verifier_python": "/abs/bin/python"}')
    assert intake.config()["verifier_python"] == "/abs/bin/python"


@pytest.mark.sandbox
@pytest.mark.verifier_account
def test_the_account_runs_the_given_python(repo):
    """A target interpreter the account cannot run makes the account unusable."""
    account = VerifierAccount()
    spec = parse_verifier("pytest", ["python3", "-m", "pytest", "tests/test_c.py"])
    head = git(repo, "rev-parse", "HEAD")
    with pytest.raises(VerifierAccountError, match="not usable"):
        run_verifiers(
            repo,
            head,
            (spec,),
            account.run_root,
            "x",
            account=account,
            python="/nonexistent/python",
        )


@pytest.mark.sandbox
def test_base_strictness_applies_and_a_missing_plugin_fails_closed(repo, tmp_path):
    base = commit_change(
        repo,
        {
            "pytest.ini": "[pytest]\naddopts = -W error::UserWarning --strict-markers\n",
            "tests/test_warns.py": "import warnings\ndef test_w():\n    warnings.warn('x')\n",
            "tests/test_mark.py": "import pytest\n@pytest.mark.undeclared\ndef test_m(): pass\n",
        },
    )
    specs = tuple(
        parse_verifier(name, ["python3", "-m", "pytest", f"tests/test_{name}.py"])
        for name in ("warns", "mark")
    )

    def passed(base_commit, run_id):
        runs = run_verifiers(repo, base, specs, tmp_path / "runs", run_id, base_commit=base_commit)
        return [run.observation.passed for run in runs]

    assert passed(None, "lenient") == [True, True]  # without the base's options both pass
    assert passed(base, "strict") == [False, False]
    if "pytest_timeout" in pytest_config.installed_plugins(sys.executable):
        pytest.skip("pytest-timeout is installed here")
    with_timeout = commit_change(repo, {"pytest.ini": "[pytest]\ntimeout = 5\n"})
    with pytest.raises(PytestConfigError, match="pytest_timeout is not installed"):
        run_verifiers(repo, with_timeout, specs, tmp_path / "runs", "t", base_commit=with_timeout)
