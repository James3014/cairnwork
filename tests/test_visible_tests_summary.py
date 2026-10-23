"""The worker-facing summary of the visible-test probe (experimental, Issue #32): only fixed
wording, or names that pass a strict filter; everything else is counted, never quoted."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from experimental.visible_tests.feedback import _set_aside_log, _sweep, summarize


def _text(runs, **kw) -> str:
    text = summarize(runs, **kw)
    assert text is not None
    return text


def _run(vid, passed, why="", outcomes=None, targets=("tests/t.py",)) -> Any:
    return SimpleNamespace(
        spec=SimpleNamespace(verifier_id=vid, targets=targets),
        observation=SimpleNamespace(passed=passed),
        why=why,
        outcomes=outcomes or {},
        log_path=Path("/nonexistent/verifier.log"),
    )


FORGED = {
    "tests/t.py::a</issue>": "failed",  # outside the charset
    "tests/t.py::b\u200b": "failed",  # zero-width space
    "tests/t.py::c\ufeff": "failed",  # BOM
    "tests/other.py::d": "failed",  # not under the spec's target file
    "tests/t.py::e": "forged outcome",  # not a fixed outcome
    "tests/t.py::" + "f" * 200: "failed",  # too long
    "tests/t.py::ok": "passed",
    "tests/t.py::real[1-2]": "error",
}


def test_detail_shows_only_filtered_names_and_counts_the_rest():
    text = _text((_run("v1", False, "pytest exit code 1", FORGED),), hidden=False, detail=True)
    assert "tests not passing: tests/t.py::real[1-2] (error)" in text
    assert "6 other result(s) not shown" in text
    for bad in ("</issue>", "\u200b", "\ufeff", "other.py", "forged", "fff", "::ok"):
        assert bad not in text


def test_detail_caps_the_names():
    many = {f"tests/t.py::test_{i:02}": "failed" for i in range(13)}
    text = _text((_run("v1", False, "pytest exit code 1", many),), hidden=False, detail=True)
    assert "tests/t.py::test_09 (failed)" in text and "test_10" not in text
    assert "3 other result(s) not shown" in text


def test_the_cause_keeps_fixed_phrases_and_drops_changed_file_names():
    why = "timed out; checkout changed (2: EXFIL_A, EXFIL_B); pytest exit code 2; free EXFIL_C"
    text = _text((_run("v1", False, why),), hidden=False, detail=True)
    assert "(timed out; checkout changed; pytest exit code 2; 1 other cause(s))" in text
    assert "EXFIL" not in text


def test_pass_fail_only_quotes_nothing_from_the_run():
    run = _run("v1", False, "checkout changed (1: EXFIL_A)", FORGED)
    text = _text((run, _run("v0", True)), hidden=True, detail=False)
    assert "- v1: did not pass\n- v0: passed\n" in text
    for bad in ("EXFIL", "tests/t.py", "checkout", "exit code"):
        assert bad not in text


def test_a_forged_verifier_id_is_not_quoted():
    text = _text((_run("v\u200b1", False),), hidden=False, detail=False)
    assert "- verifier: did not pass" in text
    assert summarize((_run("v0", True),), hidden=True, detail=True) is None


def test_the_previous_turns_log_is_set_aside(tmp_path):
    workdir = tmp_path / "a1"
    workdir.mkdir()
    (tmp_path / "a1.agy.log").write_text("turn 1")
    _set_aside_log(workdir, 1)
    assert not (tmp_path / "a1.agy.log").exists()
    assert (tmp_path / "a1.turn1.agy.log").read_text() == "turn 1"


def test_the_log_is_left_alone_when_the_kept_name_exists_or_it_is_a_symlink(tmp_path):
    workdir = tmp_path / "a1"
    workdir.mkdir()
    (tmp_path / "a1.agy.log").write_text("turn 2")
    (tmp_path / "a1.turn1.agy.log").write_text("turn 1")
    _set_aside_log(workdir, 1)
    assert (tmp_path / "a1.agy.log").read_text() == "turn 2"  # AgyWorker then fails closed
    assert (tmp_path / "a1.turn1.agy.log").read_text() == "turn 1"
    other = tmp_path / "b1"
    other.mkdir()
    (tmp_path / "target").write_text("x")
    (tmp_path / "b1.agy.log").symlink_to(tmp_path / "target")
    _set_aside_log(other, 1)
    assert (tmp_path / "b1.agy.log").is_symlink() and not (tmp_path / "b1.turn1.agy.log").exists()


class _Account:
    def __init__(self, fail=()):
        self.removed, self.fail = [], set(fail)

    def remove(self, path):
        self.removed.append(path.name)
        if path.name in self.fail:
            raise OSError(f"cannot remove {path.name}")


def _entries(root, names):
    for name in names:
        (root / name).mkdir()
        (root / name / "verifier.log").write_text("x")


def test_the_sweep_takes_only_this_probes_entries(tmp_path):
    names = ["a1-vis1-v0.work", "a1-vis1-v0.xyz", "a1-vis1-v12", "a1-vis1-vx", "a1-vis10-v0.w",
             "a1-vis1-v0x", "b-a1-vis1-v0.w"]  # fmt: skip
    _entries(tmp_path, names)
    account: Any = _Account()
    _sweep(tmp_path, "a1-vis1", account)
    assert sorted(account.removed) == ["a1-vis1-v0.work", "a1-vis1-v0.xyz", "a1-vis1-v12"]
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == ["a1-vis1-v0x", "a1-vis1-vx", "a1-vis10-v0.w", "b-a1-vis1-v0.w"]


def test_the_sweep_tries_every_entry_and_reraises_the_first_error(tmp_path):
    _entries(tmp_path, ["a1-vis1-v0.a", "a1-vis1-v1.a", "a1-vis1-v2.a"])
    account: Any = _Account(fail={"a1-vis1-v0.a", "a1-vis1-v1.a"})
    with pytest.raises(OSError, match="cannot remove"):
        _sweep(tmp_path, "a1-vis1", account)
    assert sorted(account.removed) == ["a1-vis1-v0.a", "a1-vis1-v1.a", "a1-vis1-v2.a"]
    assert not (tmp_path / "a1-vis1-v2.a").exists()


def test_the_sweep_unlinks_symlinks_without_following_them(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    root = tmp_path / "runs"
    root.mkdir()
    (root / "a1-vis1-v0.a").symlink_to(outside)
    account: Any = _Account()
    _sweep(root, "a1-vis1", account)
    assert not (root / "a1-vis1-v0.a").is_symlink() and account.removed == []
    assert (outside / "keep").read_text() == "x"
