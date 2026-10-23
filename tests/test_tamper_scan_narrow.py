"""tamper_scan narrowing (#221): each relaxation with lines that pass and lines still caught."""

import pytest
from conftest import commit_change, git

from cairnwork.tamper_scan import _is_test_path, scan_added_lines, scan_patch

TEST = "tests/test_a.py"


def _rules(path, line):
    return [f.rule for f in scan_added_lines(path, [(1, line)])]


@pytest.mark.parametrize(
    ("line", "caught"),
    [
        ("    # os._exit(0) would end the run early", False),
        ("#gc.get_objects()", False),
        ("x = 1  # os._exit(0)", True),  # code on the line: still scanned
        ("    os._exit(0)  # comment after code", True),
    ],
)
def test_pure_comment_lines_are_skipped(line, caught):
    assert bool(_rules("src/a.py", line)) is caught


@pytest.mark.parametrize(
    "line",
    [
        "sys.modules['pytest'] = fake",
        "import _pytest.runner",
        "config.pluginmanager.unregister(p)",
        "def pytest_runtest_makereport(item, call):",
        "@pytest.hookimpl(tryfirst=True)",
    ],
)
def test_pytest_rules_apply_to_test_code_only(line):
    assert _rules(TEST, line)
    assert _rules("tests/helpers/util.py", line)
    assert _rules("src/pkg/testing/plugin.py", line)
    assert _rules("src/pkg/plugin.py", line) == []
    assert _rules("app_test.py", line)


def test_other_rules_still_apply_everywhere():
    for path in ("src/a.py", TEST, "tests/conftest.py"):
        assert _rules(path, "os._exit(0)") == ["os_exit"]
        assert _rules(path, "n = os.environ['CAIRNWORK_GUARD_FDS']") == ["cairnwork_env"]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("conftest.py", True),
        ("pkg/conftest.py", True),
        ("test_x.py", True),
        ("pkg/x_test.py", True),
        ("tests/data/x.py", True),
        ("src/test/x.py", True),
        ("src/pkg/x.py", False),
        ("src/pkg/latest.py", False),
        ("src/contest_x.py", False),
        ("src/attestation.py", False),
    ],
)
def test_is_test_path(path, expected):
    assert _is_test_path(path) is expected


@pytest.mark.parametrize(
    ("line", "caught"),
    [
        ("sys.modules[__name__] = importlib.import_module('pkg._impl')", False),
        ("sys.modules[ __name__ ] = _Module()", False),
        ("sys.modules[__name__ + '.x'] = m", True),
        ("sys.modules['dep'] = MagicMock()", False),
        ('sys.modules["yaml"] = types.ModuleType("yaml")  # stub', False),
        ("sys.modules['dep.sub'] = None", False),
        ("del sys.modules['dep']", False),
        ("sys.modules.pop('dep', None)", False),
        ("sys.modules.setdefault('lancedb', types.SimpleNamespace())", False),
        ("sys.modules['pytest'] = fake", True),
        ("sys.modules['_pytest.runner'] = fake", True),
        ("sys.modules['pytest_asyncio'] = fake", True),
        ("sys.modules['pluggy'] = fake", True),
        ("sys.modules['py'] = fake", True),
        ("sys.modules['os.path'] = fake", True),
        ("sys.modules['cairnwork.verifiers'] = fake", True),
        ("sys.modules['_cairnwork_report_guard'] = fake", True),
        ("del sys.modules['pytest']", True),
        ("sys.modules.pop('_pytest.runner')", True),
        ("sys.modules.setdefault('pytest', fake)", True),
        ("sys.modules[name] = m", True),
        ("sys.modules['dep'] = m; sys.modules['pytest'] = f", True),
        ("sys.modules['dep\"] = m", True),
        ("sys.modules.update({'dep': m})", True),
        ("sys.modules.clear()", True),
        ('sys.modules["pytest"] = fake', True),
        ('sys.modules["_cairnwork_report_guard"] = fake', True),
        ("sys.modules['dep'] = sys.modules['pytest'] = Fake()", True),
        ("sys.modules['dep'] = sys.modules.pop('pytest')", True),
        ("sys.modules['dep'] = (sys.modules.update({'pytest': f}), m)[1]", True),
        ("sys.modules['dep'] = sys.modules.clear()", True),
        ("sys.modules.setdefault('dep', sys.modules.pop('pytest'))", True),
    ],
)
def test_sys_modules_stubs_of_unrelated_modules_pass(line, caught):
    assert bool(_rules(TEST, line)) is caught


@pytest.mark.parametrize(
    ("line", "caught"),
    [
        ("from _pytest.monkeypatch import MonkeyPatch", False),
        ("from _pytest.monkeypatch import MonkeyPatch as MP  # type hint", False),
        ("from _pytest.fixtures import FixtureRequest, TempPathFactory", False),
        ("from _pytest.capture import (CaptureFixture, )", False),
        ("from _pytest.monkeypatch import (MonkeyPatch,", True),  # continues on the next line
        ("from _pytest.monkeypatch import (", True),
        ("from _pytest.monkeypatch import MonkeyPatch, \\", True),
        ("from _pytest.monkeypatch import MonkeyPatch; import _pytest.runner", True),
        ("from _pytest.runner import call_and_report", True),
        ("from _pytest.nodes import Item", True),
        ("from _pytest.config import Config", True),
        ("from _pytest.reports import TestReport", True),
        ("import _pytest.monkeypatch", True),
    ],
)
def test_pytest_fixture_type_hint_imports_pass(line, caught):
    assert bool(_rules(TEST, line)) is caught


def test_a_path_with_a_space_is_still_scanned(repo):
    """git appends a tab to an unquoted ``+++ b/a b.py`` header; the path keeps no tab."""
    base = git(repo, "rev-parse", "HEAD")
    head = commit_change(repo, {"src/my file.py": "import os\nos._exit(0)\n"})
    assert [(f.path, f.rule) for f in scan_patch(repo, base, head)] == [
        ("src/my file.py", "os_exit")
    ]
