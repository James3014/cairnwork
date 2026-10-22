"""#31: recovery removes the verifier account's OWN per-attempt run directories.

When a verifier account is used, verifier clones and scratch live under the account's run root
and are owned by the account, so only ``account.remove`` can delete them. These tests use a
local stand-in for the account (no sudo); the real ``account.remove`` is covered by
test_verifier_account.py.
"""

import contextlib
import shutil
from pathlib import Path

import pytest
from conftest import commit_change
from test_recovery import GOOD, crash, stores  # reuse the crash drill

from cairnwork.recovery import _remove_account_leftovers, recover


class _FakeAccount:
    """Stand-in for the verifier account's own run root: records removals and deletes locally."""

    def __init__(self, run_root: Path) -> None:
        self._run_root = run_root
        self.removed: list[Path] = []

    @property
    def run_root(self) -> Path:
        return self._run_root

    def exclusive(self):
        return contextlib.nullcontext()

    def require_idle(self) -> None:
        pass

    def remove(self, path: Path) -> None:
        self.removed.append(Path(path))
        shutil.rmtree(path, ignore_errors=True)


def test_account_leftover_cleanup_removes_account_run_dirs(tmp_path):
    runs = tmp_path / "acct-runs"
    runs.mkdir()
    (runs / "a1-v-v0").mkdir()  # the verifier clone
    (runs / "a1-v-v0.work").mkdir()  # the verifier checkout's own dir
    verify = runs / "a1-v-v0.priv"
    (verify / "run").mkdir(parents=True)
    (verify / "verifier.log").write_text("log")
    base = runs / "a1-b-v0.priv"
    (base / "run").mkdir(parents=True)
    (runs / "b2-v-v0").mkdir()  # an unrelated attempt, must be untouched
    (runs / "b2-v-v0.work").mkdir()

    _remove_account_leftovers(_FakeAccount(runs), "a1")  # type: ignore[arg-type]

    assert not (runs / "a1-v-v0").exists()
    assert not (runs / "a1-v-v0.work").exists()
    assert not base.exists()  # a base-test run goes whole
    assert (verify / "verifier.log").exists() and not (verify / "run").exists()  # verify log kept
    assert (runs / "b2-v-v0").exists() and (runs / "b2-v-v0.work").exists()  # unrelated kept


def test_account_leftover_cleanup_ignores_a_bad_attempt_id(tmp_path):
    runs = tmp_path / "acct-runs"
    runs.mkdir()
    victim = runs / "x-v-v0"
    victim.mkdir()
    account = _FakeAccount(runs)
    _remove_account_leftovers(account, "../x")  # type: ignore[arg-type]
    assert victim.exists() and account.removed == []


def _project(repo):
    commit_change(
        repo,
        {
            "src/calc.py": "def add(a, b):\n    return a + b + 1\n",
            "tests/conftest.py": (
                "import sys, pathlib\n"
                "sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / 'src'))\n"
            ),
            "tests/test_calc.py": (
                "from calc import add\ndef test_add():\n    assert add(2, 3) == 5\n"
            ),
        },
    )
    return repo


def test_recover_cleans_the_accounts_own_run_dirs(repo, tmp_path):
    """End to end: a crashed attempt's verifier dirs under the account's run root are removed on
    recover, while an unrelated attempt's dirs there are left alone."""
    _project(repo)
    crash(repo, tmp_path, "CANDIDATE", GOOD)
    ledger, notary, journal = stores(tmp_path)
    attempt = journal.entries(ledger.in_flight()[0].task_id)[-1].attempt_id
    acct_runs = tmp_path / "acct-runs"
    acct_runs.mkdir()
    mine = acct_runs / f"{attempt}-v-v0"
    mine.mkdir()
    other = acct_runs / "zz-v-v0"
    other.mkdir()
    account = _FakeAccount(acct_runs)

    decisions = recover(
        ledger=ledger,
        notary=notary,
        journal=journal,
        run_root=tmp_path / "runs",
        verifier_account=account,  # type: ignore[arg-type]
    )

    assert [(d.new_status, d.reason) for d in decisions] == [
        ("FAILED", "INTERRUPTED_AFTER_CANDIDATE")
    ]
    assert not mine.exists() and mine in account.removed
    assert other.exists()


@pytest.mark.parametrize("tag", ["p", "q", "r"])
def test_account_leftover_cleanup_removes_p2p_run_directories(tmp_path, tag):
    """#171: the account-side remover drops the P2P -p/-q/-r dirs too."""
    runs = tmp_path / "acct-runs"
    (runs / f"a1-{tag}-v0" / "tests").mkdir(parents=True)
    (runs / f"a1-{tag}-v0.work").mkdir()
    (runs / f"a1-{tag}-v0.abc" / "run").mkdir(parents=True)
    (runs / f"a2-{tag}-v0").mkdir()

    _remove_account_leftovers(_FakeAccount(runs), "a1")  # type: ignore[arg-type]

    assert not list(runs.glob(f"a1-{tag}-*"))
    assert (runs / f"a2-{tag}-v0").exists()
