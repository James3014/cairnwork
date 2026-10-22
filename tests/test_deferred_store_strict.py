"""Strict bundle mode (``test_source: "bundle"``) accepts only a commit the private store
holds (#189): a test commit that is already in the mirror but was never imported reached the
machine by another route, so it is refused instead of being recorded as "bundle"."""

import os

import pytest
from conftest import git
from test_deferred_store import make_bundle, used_dir
from test_deferred_store import store as store  # noqa: F401 - fixture
from test_deferred_tests import REGRESSION
from test_deferred_tests import project as project  # noqa: F401 - fixture

from cairnwork.deferred_store import DeferredStore
from cairnwork.deferred_tests import TestCommitRejected, ensure_test_commit

TESTS = {"tests/test_regression.py": REGRESSION}


def _present_in_mirror(project, bundle, sha):
    git(project, "fetch", "-q", str(bundle), "refs/heads/t")  # e.g. a manual fetch or push
    assert git(project, "cat-file", "-t", sha) == "commit"


def test_strict_mode_refuses_a_commit_already_in_the_repo_but_not_imported(  # noqa: F811
    project, store, tmp_path
):
    _base, sha, bundle = make_bundle(project, TESTS, tmp_path)
    _present_in_mirror(project, bundle, sha)
    strict = DeferredStore(store.path, required=True)
    with pytest.raises(TestCommitRejected) as err:
        ensure_test_commit(project, sha, strict)
    assert err.value.code == "TEST_COMMIT_NOT_IMPORTED"


def test_auto_mode_still_accepts_a_commit_already_in_the_repo(project, store, tmp_path):  # noqa: F811
    _base, sha, bundle = make_bundle(project, TESTS, tmp_path)
    _present_in_mirror(project, bundle, sha)
    ensure_test_commit(project, sha, store)  # not required: presence is enough, as before


def test_strict_mode_accepts_an_imported_commit_already_in_the_repo(  # noqa: F811
    project, store, tmp_path
):
    _base, sha, bundle = make_bundle(project, TESTS, tmp_path)
    store.import_bundle(project, bundle, used_dir(store))
    _present_in_mirror(project, bundle, sha)
    ensure_test_commit(project, sha, DeferredStore(store.path, required=True))
    assert git(project, "cat-file", "-t", sha) == "commit"


def test_strict_mode_checks_the_store_dir_even_when_the_commit_is_present(  # noqa: F811
    project, store, tmp_path
):
    _base, sha, bundle = make_bundle(project, TESTS, tmp_path)
    store.import_bundle(project, bundle, used_dir(store))
    _present_in_mirror(project, bundle, sha)
    os.chmod(store.path, 0o750)
    try:
        with pytest.raises(TestCommitRejected) as err:
            ensure_test_commit(project, sha, DeferredStore(store.path, required=True))
    finally:
        os.chmod(store.path, 0o700)
    assert err.value.code == "TEST_STORE_UNSAFE"
