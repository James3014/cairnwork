"""The private store for deferred tests (#119): the test commit lives in a bundle under the
0700 state directory and on no remote, and every failure refuses instead of falling back."""

import os
import subprocess

import pytest
from conftest import commit_change, git
from test_deferred_tests import CHECK, FIX, REGRESSION, SeeingWorker
from test_deferred_tests import project as project  # noqa: F401 - fixture

import cairnwork.deferred_tests as dt
from cairnwork.deferred_store import DeferredStore
from cairnwork.deferred_tests import TestCommitRejected, ensure_test_commit
from cairnwork.pipeline import TaskSpec


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "state" / "deferred-tests"
    path.mkdir(parents=True, mode=0o700)
    return DeferredStore(path)


def make_bundle(project, files, tmp_path, name="supplied"):
    """A test commit that exists only in a bundle file (no remote has it)."""
    base = git(project, "rev-parse", "HEAD")
    work = tmp_path / f"author-{name}"
    git(tmp_path, "clone", "-q", "--no-local", str(project), str(work))
    git(work, "config", "user.email", "t@e")
    git(work, "config", "user.name", "t")
    git(work, "checkout", "-q", "--detach", base)
    sha = commit_change(work, files, message="tests")
    bundle = tmp_path / f"{name}.bundle"
    git(work, "update-ref", "refs/heads/t", sha)
    git(work, "bundle", "create", str(bundle), "refs/heads/t", f"^{base}")
    return base, sha, bundle


def used_dir(store):
    return store.path.parent / "verify-used-tests"


def test_import_stores_a_canonical_private_bundle(project, store, tmp_path):  # noqa: F811
    base, sha, bundle = make_bundle(project, {"tests/test_regression.py": REGRESSION}, tmp_path)
    assert store.import_bundle(project, bundle, used_dir(store)) == sha
    assert store.has(sha) and (store.bundle(sha).stat().st_mode & 0o777) == 0o600
    assert store.parent(sha, project) == base
    assert not any(p.name.startswith(".") for p in store.path.iterdir())  # scratch removed
    assert git(project, "remote") == ""  # no remote exists that could hold it


def test_the_commit_is_not_on_the_mirror_until_fetched_after_the_worker(project, store, tmp_path):  # noqa: F811
    _base, sha, bundle = make_bundle(project, {"tests/test_regression.py": REGRESSION}, tmp_path)
    store.import_bundle(project, bundle, used_dir(store))
    dt.never_seen(project, sha)  # imported, still absent from the mirror
    ensure_test_commit(project, sha, store)
    assert git(project, "cat-file", "-t", sha) == "commit"


def test_import_twice_refuses_and_changes_nothing(project, store, tmp_path):  # noqa: F811
    _b, sha, bundle = make_bundle(project, {"tests/test_regression.py": REGRESSION}, tmp_path)
    store.import_bundle(project, bundle, used_dir(store))
    before = store.bundle(sha).read_bytes()
    with pytest.raises(TestCommitRejected) as err:
        store.import_bundle(project, bundle, used_dir(store))
    assert err.value.code == "TEST_BUNDLE_EXISTS"
    assert store.bundle(sha).read_bytes() == before


def test_a_burned_or_exposed_commit_is_not_imported(project, store, tmp_path):  # noqa: F811
    _b, sha, bundle = make_bundle(project, {"tests/test_regression.py": REGRESSION}, tmp_path)
    used = used_dir(store)
    used.mkdir()
    (used / sha).touch()
    with pytest.raises(TestCommitRejected, match="ALREADY_EXPOSED"):
        store.import_bundle(project, bundle, used)
    (used / sha).unlink()
    git(project, "fetch", "-q", str(bundle), "refs/heads/t")  # now the mirror holds it
    with pytest.raises(TestCommitRejected, match="ALREADY_EXPOSED"):
        store.import_bundle(project, bundle, used)
    assert not store.has(sha)


def test_a_bundle_with_two_heads_or_a_merge_is_refused(project, store, tmp_path):  # noqa: F811
    base, _sha, _bundle = make_bundle(project, {"tests/test_a.py": REGRESSION}, tmp_path, "a")
    work = tmp_path / "author-a"
    other = commit_change(work, {"tests/test_b.py": REGRESSION}, message="two")
    git(work, "update-ref", "refs/heads/u", other)
    two = tmp_path / "two.bundle"
    git(work, "bundle", "create", str(two), "refs/heads/t", "refs/heads/u", f"^{base}")
    with pytest.raises(TestCommitRejected, match="TEST_BUNDLE_SHAPE"):
        store.import_bundle(project, two, used_dir(store))
    one = tmp_path / "chain.bundle"  # head two commits above the base: parent is not the base
    git(work, "bundle", "create", str(one), "refs/heads/u", f"^{base}")
    # u's parent is the first test commit, a stranger to the mirror: prerequisite is missing
    with pytest.raises(TestCommitRejected):
        store.import_bundle(project, one, used_dir(store))
    assert list(store.path.iterdir()) == []


def test_a_garbage_bundle_is_refused(project, store, tmp_path):  # noqa: F811
    junk = tmp_path / "junk.bundle"
    junk.write_text("not a bundle")
    with pytest.raises(TestCommitRejected):
        store.import_bundle(project, junk, used_dir(store))
    assert list(store.path.iterdir()) == []


def test_a_store_anyone_else_can_enter_is_refused(project, store, tmp_path):  # noqa: F811
    _b, sha, bundle = make_bundle(project, {"tests/test_regression.py": REGRESSION}, tmp_path)
    store.import_bundle(project, bundle, used_dir(store))
    os.chmod(store.path, 0o750)
    with pytest.raises(TestCommitRejected, match="TEST_STORE_UNSAFE"):
        ensure_test_commit(project, sha, store)
    with pytest.raises(TestCommitRejected, match="TEST_STORE_UNSAFE"):
        store.parent(sha, project)


def test_a_symlinked_store_is_refused(project, tmp_path):  # noqa: F811
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(TestCommitRejected, match="TEST_STORE_UNSAFE"):
        DeferredStore(link).check_dir()


def test_a_required_store_never_falls_back_to_origin(project, store, tmp_path):  # noqa: F811
    strict = DeferredStore(store.path, required=True)
    with pytest.raises(TestCommitRejected, match="TEST_COMMIT_NOT_IMPORTED"):
        ensure_test_commit(project, "a" * 40, strict)


def test_a_corrupt_stored_bundle_is_unreadable_not_success(project, store, tmp_path):  # noqa: F811
    _b, sha, bundle = make_bundle(project, {"tests/test_regression.py": REGRESSION}, tmp_path)
    store.import_bundle(project, bundle, used_dir(store))
    store.bundle(sha).write_bytes(b"corrupt")
    with pytest.raises(TestCommitRejected, match="TEST_COMMIT_UNREADABLE"):
        ensure_test_commit(project, sha, store)
    assert subprocess.run(
        ["git", "-C", str(project), "cat-file", "-e", sha], check=False
    ).returncode


@pytest.mark.sandbox
def test_the_pipeline_judges_tests_that_were_never_on_any_remote(project, tmp_path):  # noqa: F811
    base, sha, bundle = make_bundle(project, {"tests/test_regression.py": REGRESSION}, tmp_path)
    state = tmp_path / "state"
    store = DeferredStore(state / "deferred-tests", required=True)
    store.path.mkdir(parents=True, mode=0o700)
    store.import_bundle(project, bundle, used_dir(store))
    worker = SeeingWorker(FIX)
    task = TaskSpec("k", "card-k", "fix add", "x", project, base, ("src/calc.py",), CHECK,
                    test_commit=sha, test_store=store)  # fmt: skip
    from cairnwork.journal import Journal
    from cairnwork.notary import Notary
    from cairnwork.ownership import OwnershipLedger
    from cairnwork.pipeline import run_task

    result = run_task(
        task,
        ledger=OwnershipLedger(state / "own.sqlite"),
        notary=Notary(state / "notary.sqlite"),
        journal=Journal(state / "journal.sqlite"),
        worker=worker,
        run_root=state / "runs",
    )
    assert result.status == "COMPLETED", result.reason
    assert worker.saw_test is False
    assert git(project, "remote") == ""  # there was no origin to fetch from


def test_the_cli_imports_a_bundle_and_refuses_a_second_import(project, tmp_path):  # noqa: F811
    from verify_helpers import cli

    state = tmp_path / "cli-state"
    state.mkdir(mode=0o700)
    base, sha, bundle = make_bundle(project, {"tests/test_regression.py": REGRESSION}, tmp_path)
    args = ("import-tests", "--repo-path", str(project), "--bundle", str(bundle))
    assert cli(state, *args) == (0, {"imported": sha})
    assert (state / "deferred-tests" / f"{sha}.bundle").is_file()
    code, out = cli(state, *args)
    assert code == 2 and out["error"] == "TEST_BUNDLE_EXISTS"
    assert base  # the parent was read from the mirror, not from the bundle
