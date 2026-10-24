"""Issue #240: removing a verifier's checkout and scratch. The verdict is decided before cleanup,
so a failing removal (the account's included) never escapes run_verifiers; what the owner cannot
remove is reported as an ALERT on stderr, never silent. No sudo: stand-in accounts."""

import os
import subprocess

import pytest

import cairnwork.verifiers as verifiers
from cairnwork.account_checkout import STAGE
from cairnwork.verifier_account import VerifierAccountError


@pytest.mark.parametrize(
    "error", [VerifierAccountError("outside"), subprocess.TimeoutExpired("rm", 60), OSError("sudo")]
)
def test_a_failing_account_removal_keeps_the_verdict_and_the_owner_removes_its_part(
    tmp_path, capsys, error
):
    """The verdict is decided before cleanup: a failing account removal is not raised out of
    run_verifiers; the owner removes what it can, and what is left would be an ALERT."""
    work = tmp_path / "x.work"
    (work / STAGE / "x").mkdir(parents=True)
    (work / STAGE / "x" / "f").write_text("owner")

    class Failing:
        def remove(self, path):
            raise error

    verifiers._remove_checkout(work, "x", Failing())
    assert not work.exists() and "ALERT" not in capsys.readouterr().err


def test_a_failing_account_removal_of_scratch_keeps_the_verdict(tmp_path):
    home, tmp = tmp_path / "home", tmp_path / "tmp"
    for directory in (home, tmp):
        directory.mkdir()
        (directory / "f").write_text("x")

    class Failing:
        def remove(self, path):
            raise subprocess.TimeoutExpired("rm", 60)

    verifiers._remove_scratch((home, tmp), Failing())
    assert not home.exists() and not tmp.exists()


def test_a_scratch_leftover_is_reported_not_silent(tmp_path, capsys):
    home, tmp = tmp_path / "home", tmp_path / "tmp"
    closed = home / "closed"  # stands in for what only the account could remove
    closed.mkdir(parents=True)
    (closed / "f").write_text("account")
    tmp.mkdir()
    os.chmod(closed, 0o500)

    class Failing:
        def remove(self, path):
            raise VerifierAccountError("sudo failed")

    try:
        verifiers._remove_scratch((home, tmp), Failing())
        assert home.exists() and not tmp.exists()
        err = capsys.readouterr().err
        assert f"ALERT verifier cleanup: {home} was not removed" in err and str(tmp) not in err
    finally:
        os.chmod(closed, 0o700)


def test_a_leftover_after_cleanup_is_reported_not_silent(tmp_path, capsys):
    """An account whose removal did nothing (``rm`` runs with check=False) leaves its copy, which
    the owner cannot remove: the run's verdict stands, but the leftover is an ALERT on stderr."""
    work = tmp_path / "x.work"
    copy = work / "x" / "closed"  # stands in for the account's copy: the owner's rmtree fails
    copy.mkdir(parents=True)
    (copy / "f").write_text("account")
    os.chmod(copy, 0o500)

    class Inert:
        def remove(self, path):
            pass

    try:
        verifiers._remove_checkout(work, "x", Inert())
        assert work.exists()
        assert f"ALERT verifier cleanup: {work} was not removed" in capsys.readouterr().err
    finally:
        os.chmod(copy, 0o700)
    verifiers._remove_checkout(work, "x", Inert())
    assert not work.exists() and "ALERT" not in capsys.readouterr().err
