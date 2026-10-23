"""The patch scan for verdict-forgery APIs (tamper_scan.py): a catch and a non-catch per rule."""

import pytest
from conftest import commit_change, git

from cairnwork.tamper_scan import CODE, RULES, added_lines, scan_added_lines, scan_patch

# rule name -> (a line that must be caught, a similar line that must pass)
CASES = {
    "os_exit": ("    os._exit(0)", "    os.exit_code = 0  # not os._exit_stack or sys.exit"),
    "cairnwork_env": (
        "    n = os.environ.get('CAIRNWORK_GUARD_FDS')",
        "    home = os.environ['HOME']  # the name CAIRNWORK is fine without environ",
    ),
    "gc_walk": ("    objs = gc.get_objects()", "    gc.collect()"),
    "sys_modules_write": (
        "    sys.modules['pytest'] = fake",
        "    mod = sys.modules.get('json')  # reading is fine; so is == compare: sys.modules == {}",
    ),
    "pytest_internals": (
        "    import _pytest.runner",
        "    import pytest  # my_pytest_helper is a different name",
    ),
    "pytest_hook_impl": (
        "def pytest_runtest_makereport(item, call):",
        "def test_pytest_runtest_makereport_name():",
    ),
    "guard_module": ("import _cairnwork_report_guard", "import cairnwork.report_guard_docs"),
    "fd_listing": (
        "    names = os.listdir('/proc/self/fd')",
        "    p = '/proc/cpuinfo'  # other /proc files and /dev/null are fine",
    ),
}


def test_every_rule_has_a_case():
    assert {rule.name for rule in RULES} == set(CASES)


TESTS_ONLY = {rule.name for rule in RULES if rule.tests_only}


@pytest.mark.parametrize("rule", sorted(CASES))
def test_a_rule_catches_its_api_and_not_a_lookalike(rule):
    bad, good = CASES[rule]
    path = "tests/test_a.py" if rule in TESTS_ONLY else "src/a.py"
    assert [f.rule for f in scan_added_lines(path, [(3, bad)])] == [rule]
    assert scan_added_lines(path, [(3, good)]) == []


def test_more_forms_of_the_same_rules_are_caught():
    forms = [
        "from os import path, _exit",
        "os . _exit(1)",
        "v = os.getenv('CAIRNWORK_VERDICT_NONCE')",
        "gc.get_referrers(x)",
        "del sys.modules['pytest']",
        "sys.modules.pop('_pytest.runner')",
        "def f(config): config.pluginmanager.unregister(p)",
        "@pytest.hookimpl(tryfirst=True)",
        "open('/dev/fd/3')",
    ]
    for form in forms:
        assert scan_added_lines("tests/test_a.py", [(1, form)]), form


def test_hook_implementations_are_normal_in_a_conftest_but_other_rules_still_apply():
    hook = (1, "def pytest_configure(config):")
    assert scan_added_lines("tests/conftest.py", [hook]) == []
    assert scan_added_lines("tests/test_a.py", [hook])
    assert scan_added_lines("tests/conftest.py", [(2, "os._exit(0)")])


def test_only_python_files_are_scanned():
    assert scan_added_lines("docs/notes.md", [(1, "os._exit(0)")]) == []


def test_added_lines_reads_new_paths_and_line_numbers():
    diff = (
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
        "@@ -1,0 +5,2 @@\n+one\n+two\n"
        "diff --git a/src/gone.py b/src/gone.py\n--- a/src/gone.py\n+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-x\n"
    )
    assert added_lines(diff) == {"src/a.py": [(5, "one"), (6, "two")]}


def test_scan_patch_reads_only_added_lines_of_a_real_commit(repo):
    base = git(repo, "rev-parse", "HEAD")
    commit_change(repo, {"src/old.py": "import os\nos._exit(0)\n"})
    mid = git(repo, "rev-parse", "HEAD")  # the removal below must not count
    head = commit_change(
        repo, {"src/old.py": "import os\nx = 1\n", "src/new.py": "gc.get_objects()\n"}
    )
    assert scan_patch(repo, mid, head)[0].rule == "gc_walk"
    assert [(f.path, f.line, f.rule) for f in scan_patch(repo, base, mid)] == [
        ("src/old.py", 2, "os_exit")
    ]
    assert CODE == "VERIFIER_TAMPER_API"


def test_paths_git_quotes_are_still_scanned(repo):
    """git C-quotes paths with special or non-ASCII characters (``+++ "b/..."``): still scanned."""
    base = git(repo, "rev-parse", "HEAD")
    head = commit_change(
        repo, {'src/we"ird.py': "import os\nos._exit(0)\n", "src/café.py": "gc.get_objects()\n"}
    )
    found = {(f.path, f.rule) for f in scan_patch(repo, base, head)}
    assert found == {('src/we"ird.py', "os_exit"), ("src/café.py", "gc_walk")}
