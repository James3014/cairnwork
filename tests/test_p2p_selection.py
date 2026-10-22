"""Unit tests for the pure P2P test selection (#117)."""

from __future__ import annotations

import pytest

from cairnwork.p2p_selection import DEFAULT_MODE, MODES, check_p2p_config
from cairnwork.p2p_selection import select_p2p_targets as _select

ROOTS = ("tests",)


def select_p2p_targets(changed, existing, roots, mode="module-dir"):
    return _select(changed, existing, roots, mode)  # these tests default to the narrower mode


# a small flat suite plus a nested sub-package suite
SUITE = (
    "tests/test_notary.py",
    "tests/test_pipeline.py",
    "tests/sub/test_mod.py",
    "tests/sub/test_other.py",
)


def test_default_mode_is_whole_suite():
    assert DEFAULT_MODE == "whole-suite"
    assert set(MODES) == {"off", "whole-suite", "module-dir"}


def test_off_selects_nothing():
    assert select_p2p_targets(("src/cairnwork/notary.py",), SUITE, ROOTS, "off") == ()


def test_whole_suite_selects_everything_sorted():
    assert select_p2p_targets(("src/cairnwork/notary.py",), SUITE, ROOTS, "whole-suite") == tuple(
        sorted(SUITE)
    )


def test_empty_existing_tests_selects_nothing():
    assert select_p2p_targets(("src/cairnwork/notary.py",), (), ROOTS, "whole-suite") == ()
    assert select_p2p_targets(("src/cairnwork/notary.py",), (), ROOTS) == ()


def test_name_match_picks_the_named_test_and_its_directory():
    # src/cairnwork/notary.py -> tests/test_notary.py by name; the siblings in tests/ come along
    out = select_p2p_targets(("src/cairnwork/notary.py",), SUITE, ROOTS)
    assert out == ("tests/test_notary.py", "tests/test_pipeline.py")


def test_name_match_suffix_form():
    suite = ("tests/notary_test.py", "tests/test_other.py", "tests/sub/test_deep.py")
    out = select_p2p_targets(("src/cairnwork/notary.py",), suite, ROOTS)
    assert out == ("tests/notary_test.py", "tests/test_other.py")


def test_mirror_dir_match_for_subpackage():
    # src/pkg/sub/mod.py -> strip 'src', drop top package 'pkg' -> mirror dir tests/sub
    out = select_p2p_targets(("src/pkg/sub/mod.py",), SUITE, ROOTS)
    assert set(out) == {"tests/sub/test_mod.py", "tests/sub/test_other.py"}


def test_mirror_and_name_union():
    # mod.py matches tests/sub/* by mirror AND tests/sub/test_mod.py by name -> union, deduped
    out = select_p2p_targets(("src/pkg/sub/mod.py",), SUITE, ROOTS)
    assert out == tuple(sorted(set(out)))
    assert "tests/sub/test_mod.py" in out


def test_fallback_to_root_level_tests_when_no_match():
    # a changed file nothing maps to -> the tests directly in the root (not nested) are selected
    out = select_p2p_targets(("src/cairnwork/unmatched.py",), SUITE, ROOTS)
    assert set(out) == {"tests/test_notary.py", "tests/test_pipeline.py"}


def test_changed_test_file_is_not_a_source():
    # a changed file that is itself under a test root contributes nothing as a source
    assert select_p2p_targets(("tests/test_notary.py",), SUITE, ROOTS) == ()


def test_multiple_changed_files_union():
    out = select_p2p_targets(("src/cairnwork/notary.py", "src/pkg/sub/mod.py"), SUITE, ROOTS)
    assert "tests/test_notary.py" in out
    assert "tests/sub/test_mod.py" in out
    assert out == tuple(sorted(set(out)))


def test_a_flat_test_directory_brings_in_the_siblings_of_the_matched_test():
    # pkg/mod.py -> tests/test_mod.py by name; its sibling in the same flat directory is the
    # regression #117 is about, so it is selected too (nested directories stay out).
    suite = ("tests/test_a.py", "tests/test_mod.py", "tests/sub/test_deep.py")
    out = select_p2p_targets(("pkg/mod.py",), suite, ROOTS)
    assert out == ("tests/test_a.py", "tests/test_mod.py")


def test_config_check():
    check_p2p_config({})
    check_p2p_config({"p2p_selection": "off", "p2p_time_budget_seconds": 86400})
    for bad in (
        {"p2p_selection": "x"},
        {"p2p_time_budget_seconds": 0},
        {"p2p_time_budget_seconds": True},
        {"p2p_time_budget_seconds": "5"},
    ):
        with pytest.raises(ValueError):
            check_p2p_config(bad)


def test_unknown_mode_raises():
    with pytest.raises(ValueError, match="unknown p2p_selection mode"):
        select_p2p_targets(("src/cairnwork/notary.py",), SUITE, ROOTS, "bogus")


def test_multiple_roots():
    roots = ("tests", "pkg/tests")
    suite = ("tests/test_notary.py", "pkg/tests/test_notary.py")
    out = select_p2p_targets(("src/cairnwork/notary.py",), suite, roots)
    assert set(out) == {"tests/test_notary.py", "pkg/tests/test_notary.py"}
