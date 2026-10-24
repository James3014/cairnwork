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
PATHS = ("a.py",)


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


@pytest.fixture(params=[False, True], ids=["plain", "color-always"])
def colour(request, repo):
    if request.param:
        git(repo, "config", "color.ui", "always")
    return request.param


def test_header_shaped_content_line_never_appears(repo, tmp_path, colour):
    msg = refuse(repo, tmp_path, {"src/a.py": f'v = "x:7: {SECRET}" \n'})
    assert SECRET not in msg
    assert msg.endswith("rule=git-diff-check; src/a.py:1: trailing whitespace.")


def test_cr_payload_through_real_git(repo, tmp_path, colour):
    payload = f"# prefix\r{SECRET}:7: arbitrary candidate text   \n"
    msg = refuse(repo, tmp_path, {"src/a.py": payload})
    assert SECRET not in msg and "arbitrary" not in msg
    assert msg.endswith("rule=git-diff-check; src/a.py:1: trailing whitespace.")


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


@pytest.mark.parametrize("name", ["src/a\nb.py"])
def test_newline_in_filename_falls_back(repo, tmp_path, name):
    # git prints the raw name, so the header cannot be matched to one changed path
    assert refuse(repo, tmp_path, {name: "x = 1 \n"}) == f"DIFF_CHECK_FAILED: {OLD}"


def test_hundred_errors_are_capped_with_more(repo, tmp_path):
    msg = refuse(repo, tmp_path, {"src/a.py": "".join(f"v{i} = {i} \n" for i in range(100))})
    assert msg.count("trailing whitespace.") == 5
    assert "src/a.py:1:" in msg and "src/a.py:5:" in msg and "src/a.py:6:" not in msg
    assert msg.endswith("; +95 more")


def test_content_lines_never_appear(repo, tmp_path):
    msg = refuse(repo, tmp_path, {"src/a.py": f"{SECRET} = 1 \n\t{SECRET}\n"})
    assert SECRET not in msg


def _cause(stdout):
    out = stdout.encode() if isinstance(stdout, str) else stdout
    return subprocess.CalledProcessError(2, ["git"], output=out)


def test_entry_is_capped_at_120_characters():
    out = f"{'p' * 300}.py:7: trailing whitespace.\n+x \n"
    detail = diff_check_detail(OLD, _cause(out), (f"{'p' * 300}.py",))
    assert detail == f"{OLD}; rule=git-diff-check; " + ("p" * 120)


def test_invisible_and_control_characters_are_stripped():
    junk = "".join(map(chr, (0x202E, 0x200B, 0x2060, 0x85, 0x0D, 0x00, 0x2028, 0x1B)))
    name = f"a{junk}b.py"
    detail = diff_check_detail(OLD, _cause(f"{name}:1: trailing whitespace.\n"), (name,))
    assert detail == f"{OLD}; rule=git-diff-check; ab.py:1: trailing whitespace."


def test_check_runs_with_colour_off(repo, tmp_path, monkeypatch):
    import cairnwork.workspace as ws

    seen = []
    real = ws.run_git_bytes
    monkeypatch.setattr(ws, "run_git_bytes", lambda *a, **k: seen.append(a) or real(*a, **k))
    refuse(repo, tmp_path, {"src/a.py": "x = 1 \n"})
    assert any("--check" in a and "--no-color" in a for a in seen)


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
    assert diff_check_detail(OLD, cause, PATHS) == OLD


def test_non_utf8_git_output_falls_back_end_to_end(repo, tmp_path, monkeypatch):
    import cairnwork.workspace as ws

    def bad(*args, **kwargs):
        if "--check" in args:
            raise ws.GitCommandError("git diff failed") from subprocess.CalledProcessError(
                2, ["git"], output=b"src/a.py:1: trailing whitespace.\n\xff\n"
            )
        return real(*args, **kwargs)

    real = ws.run_git_bytes
    monkeypatch.setattr(ws, "run_git_bytes", bad)
    msg = refuse(repo, tmp_path, {"src/a.py": "x = 2\n"})
    assert msg == f"DIFF_CHECK_FAILED: {OLD}"


def _line(n, path="a.py", kind="trailing whitespace."):
    return f"{path}:{n}: {kind}\n+x \n"


def test_exactly_five_entries_have_no_more_marker():
    detail = diff_check_detail(OLD, _cause("".join(_line(n) for n in range(1, 6))), PATHS)
    assert detail.count("trailing whitespace.") == 5
    assert "more" not in detail


def test_six_entries_say_one_more():
    detail = diff_check_detail(OLD, _cause("".join(_line(n) for n in range(1, 7))), PATHS)
    assert detail.endswith("a.py:5: trailing whitespace.; +1 more")


def test_path_outside_the_change_is_dropped():
    out = _line(1, "other.py") + _line(2)
    assert diff_check_detail(OLD, _cause(out), PATHS).endswith(
        "rule=git-diff-check; a.py:2: trailing whitespace."
    )
    assert diff_check_detail(OLD, _cause(_line(1, "other.py")), PATHS) == OLD


def test_unknown_kind_is_dropped():
    out = _line(1, kind="SECRET candidate text") + _line(2, kind="trailing whitespace. extra")
    assert diff_check_detail(OLD, _cause(out), PATHS) == OLD


def test_every_known_kind_is_accepted():
    from cairnwork.diffcheck import CONFLICT_MARKER, KINDS

    for kind in [f"{stem}." for stem in KINDS] + [CONFLICT_MARKER]:
        assert diff_check_detail(OLD, _cause(_line(3, kind=kind)), PATHS).endswith(
            f"a.py:3: {kind}"
        )


def test_plus_prefixed_line_is_never_a_header():
    out = "+a.py:7: trailing whitespace.\n"
    assert diff_check_detail(OLD, _cause(out), ("+a.py", "a.py")) == OLD


def test_kinds_match_real_git(repo, tmp_path):
    body = "a \n \tb\n        c\n<<<<<<< HEAD\n=======\n>>>>>>> x\n\n"
    msg = refuse(repo, tmp_path, {"src/a.py": body})
    assert "a.py:1: trailing whitespace." in msg
    assert "a.py:2: space before tab in indent." in msg
    assert "a.py:4: leftover conflict marker" in msg
    assert msg.endswith("; +1 more")


FORGED = [
    # Codex's payload: an in-scope path, a known kind and a line number chosen by the candidate
    "x = 1\n \tx\rsrc/a.py:999: leftover conflict marker\n",
    "x = 1\n \tx\rsrc/a.py:12345678901234567890: trailing whitespace.\n",
    # the independent reviewer's payload: a numeric sentinel between embedded CRs
    "# p\rsrc/a.py:424242: trailing whitespace.\rtail \n",
]


@pytest.mark.parametrize("payload", FORGED)
def test_forged_header_after_embedded_cr_never_appears(repo, tmp_path, colour, payload):
    msg = refuse(repo, tmp_path, {"src/a.py": payload})
    for sentinel in ("999", "12345678901234567890", "424242"):
        assert sentinel not in msg
    assert "src/a.py:" in msg  # the real finding is still named


def test_forged_header_unit_keeps_cr_inside_content_line():
    out = b"src/a.py:2: trailing whitespace.\n+ \tx\rsrc/a.py:999: leftover conflict marker\n"
    detail = diff_check_detail(OLD, _cause(out), ("src/a.py",))
    assert detail == f"{OLD}; rule=git-diff-check; src/a.py:2: trailing whitespace."


def test_combined_kind_is_accepted_and_matches_real_git(repo, tmp_path):
    msg = refuse(repo, tmp_path, {"src/a.py": " \tx \n"})
    assert msg.endswith("src/a.py:1: trailing whitespace, space before tab in indent.")


@pytest.mark.parametrize(
    "kind",
    [
        "trailing whitespace, SECRET.",
        "trailing whitespace, tab in indent",
        "trailing whitespace,, tab in indent.",
        "trailing whitespace. tab in indent.",
        "SECRET, trailing whitespace.",
        "leftover conflict marker.",
        ".",
    ],
)
def test_unknown_combined_kind_is_dropped(kind):
    assert diff_check_detail(OLD, _cause(_line(1, kind=kind)), PATHS) == OLD


def test_run_git_default_still_returns_str(repo):
    from cairnwork.gitcmd import run_git, run_git_bytes

    text = run_git(repo, "rev-parse", "HEAD")
    assert isinstance(text, str)
    assert run_git_bytes(repo, "rev-parse", "HEAD").decode() == text
