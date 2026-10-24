"""DIFF_CHECK_FAILED names the rule and path:line, never candidate text (#227)."""

import subprocess

import pytest
from conftest import git

from cairnwork.diffcheck import diff_check_detail
from cairnwork.workspace import (
    Attempt,
    CandidateRejected,
    _guarded_refs,
    collect_candidate,
    create_attempt_clone,
)

OLD = "whitespace errors in the change"
WORKER = ("-c", "user.email=w@example.invalid", "-c", "user.name=w")
SECRET = "SECRETCONTENT"


def refuse(repo, tmp_path, files):
    base = git(repo, "rev-parse", "HEAD")
    clone = create_attempt_clone(repo, base, tmp_path / "attempts", "a1").path
    for name, text in files.items():
        (clone / name).parent.mkdir(parents=True, exist_ok=True)
        (clone / name).write_text(text)
    git(clone, "add", "-A")
    git(clone, *WORKER, "commit", "-q", "-m", "candidate")
    made = Attempt("a1", base, clone, _guarded_refs(repo, "a1"))
    with pytest.raises(CandidateRejected) as info:
        collect_candidate(repo, made, tuple(files), allow_deletions=True)
    assert info.value.code == "DIFF_CHECK_FAILED"
    return str(info.value)


def test_trailing_space_names_path_line_and_kind(repo, tmp_path):
    msg = refuse(repo, tmp_path, {"src/a.py": f"x = 1\ny = 2   # {SECRET}   \n"})
    assert msg == f"DIFF_CHECK_FAILED: {OLD}; rule=git-diff-check; src/a.py:2: trailing whitespace."
    assert SECRET not in msg


def test_blank_line_at_eof(repo, tmp_path):
    msg = refuse(repo, tmp_path, {"src/a.py": "x = 1\n\n"})
    assert msg.endswith("rule=git-diff-check; src/a.py:2: new blank line at EOF.")


def test_ansi_escape_in_filename_is_stripped(repo, tmp_path):
    msg = refuse(repo, tmp_path, {"src/\x1b[31mred.py": "x = 1 \n"})
    assert "\x1b" not in msg
    assert "red.py:1: trailing whitespace." in msg


def test_newline_in_filename_adds_no_line(repo, tmp_path):
    msg = refuse(repo, tmp_path, {"src/a\nb.py": "x = 1 \n"})
    assert "\n" not in msg
    assert msg.startswith(f"DIFF_CHECK_FAILED: {OLD}; rule=git-diff-check; ")
    assert "trailing whitespace." in msg


def test_hundred_errors_are_capped_with_more(repo, tmp_path):
    msg = refuse(repo, tmp_path, {"src/a.py": "".join(f"v{i} = {i} \n" for i in range(100))})
    assert msg.count("trailing whitespace.") == 5
    assert "src/a.py:1:" in msg and "src/a.py:5:" in msg and "src/a.py:6:" not in msg
    assert msg.endswith("; +95 more")


def test_content_lines_never_appear(repo, tmp_path):
    msg = refuse(repo, tmp_path, {"src/a.py": f"{SECRET} = 1 \n\t{SECRET}\n"})
    assert SECRET not in msg


def _cause(stdout):
    return subprocess.CalledProcessError(2, ["git"], output=stdout)


def test_entry_is_capped_at_120_characters():
    out = f"{'p' * 300}.py:7: trailing whitespace.\n+x \n"
    detail = diff_check_detail(OLD, _cause(out))
    assert detail == f"{OLD}; rule=git-diff-check; " + ("p" * 120)


def test_bidi_characters_are_stripped():
    out = "a‮bc.py:1: trailing whitespace.\n"
    assert "‮" not in diff_check_detail(OLD, _cause(out))


@pytest.mark.parametrize(
    "cause",
    [
        None,
        ValueError("x"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"),
        subprocess.CalledProcessError(2, ["git"], output=b"\xff\xfe:1: x"),
        _cause(""),
        _cause("+only content\nnot a header\n"),
    ],
)
def test_undecodable_or_unparsable_output_falls_back_to_old_text(cause):
    assert diff_check_detail(OLD, cause) == OLD


def test_non_utf8_git_output_falls_back_end_to_end(repo, tmp_path, monkeypatch):
    import cairnwork.workspace as ws

    def bad(*args, **kwargs):
        if "--check" in args:
            raise ws.GitCommandError("git diff failed") from UnicodeDecodeError(
                "utf-8", b"\xff", 0, 1, "bad"
            )
        return real(*args, **kwargs)

    real = ws.run_git
    monkeypatch.setattr(ws, "run_git", bad)
    msg = refuse(repo, tmp_path, {"src/a.py": "x = 2\n"})
    assert msg == f"DIFF_CHECK_FAILED: {OLD}"
