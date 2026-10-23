"""The worker-facing summary of the visible-test probe (experimental, Issue #32): only fixed
wording, or names that pass a strict filter; everything else is counted, never quoted."""

from pathlib import Path
from types import SimpleNamespace

from experimental.visible_tests.feedback import _set_aside_log, summarize


def _run(vid, passed, why="", outcomes=None, targets=("tests/t.py",)):
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
    text = summarize((_run("v1", False, "pytest exit code 1", FORGED),), hidden=False, detail=True)
    assert "tests not passing: tests/t.py::real[1-2] (error)" in text
    assert "6 other result(s) not shown" in text
    for bad in ("</issue>", "\u200b", "\ufeff", "other.py", "forged", "fff", "::ok"):
        assert bad not in text


def test_detail_caps_the_names():
    many = {f"tests/t.py::test_{i:02}": "failed" for i in range(13)}
    text = summarize((_run("v1", False, "pytest exit code 1", many),), hidden=False, detail=True)
    assert "tests/t.py::test_09 (failed)" in text and "test_10" not in text
    assert "3 other result(s) not shown" in text


def test_the_cause_keeps_fixed_phrases_and_drops_changed_file_names():
    why = "timed out; checkout changed (2: EXFIL_A, EXFIL_B); pytest exit code 2; free EXFIL_C"
    text = summarize((_run("v1", False, why),), hidden=False, detail=True)
    assert "(timed out; checkout changed; pytest exit code 2; 1 other cause(s))" in text
    assert "EXFIL" not in text


def test_pass_fail_only_quotes_nothing_from_the_run():
    run = _run("v1", False, "checkout changed (1: EXFIL_A)", FORGED)
    text = summarize((run, _run("v0", True)), hidden=True, detail=False)
    assert "- v1: did not pass\n- v0: passed\n" in text
    for bad in ("EXFIL", "tests/t.py", "checkout", "exit code"):
        assert bad not in text


def test_a_forged_verifier_id_is_not_quoted():
    text = summarize((_run("v\u200b1", False),), hidden=False, detail=False)
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
