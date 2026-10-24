"""The write scope in the loop, the CLI, the manifest and the corpus line (#88 follow-up)."""

import json

import pytest
from local_worker_fakes import META, TASK, cli_harness, lines, make_loop, reply

from experimental.local_worker import episode, write_scope
from experimental.local_worker.agent import initial_messages
from experimental.local_worker.episode_schema import (
    SchemaError,
    _check_write_scope,
    validate_record,
)
from experimental.local_worker.redact import path_digest

OUT_OF_SCOPE = reply("write_file", path="tests/test_app.py", content="def test(): 1\n")
IN_SCOPE = reply("edit_file", path="src/app.py", target="x = 1", replacement="x = 2")


def _record(tmp_path, replies, scope):
    loop, world, _, _ = make_loop(tmp_path, replies, write_scope=scope)
    out = loop.run(initial_messages(TASK))
    rec = episode.build_record(META, out, write_scope=loop.settings.write_scope)
    validate_record(rec)
    return rec, world


def test_a_refused_turn_is_recorded_and_the_next_one_succeeds(tmp_path):
    rec, world = _record(tmp_path, [OUT_OF_SCOPE, IN_SCOPE], ["src/"])
    refused, edited, finish = rec["turns"]
    assert refused["error_category"] == "REFUSED" and refused["exit_code"] == 64
    assert refused["tree_differs_from_base"] is False  # nothing was written
    assert edited["error_category"] == "NONE" and edited["exit_code"] == 0
    assert finish["action_type"] == "finish" and rec["verification"]["exit_reason"] == "COMPLETED"
    assert rec["first_edit_cut"]["cut_turn_index"] == 1
    assert world.files["tests/test_app.py"] == world.base["tests/test_app.py"]
    assert rec["write_scope"] == ["src"]  # a base path: readable
    writes = [c[1] for c in world.calls if c[0] == "write"]
    assert writes == [["tests/test_app.py", "--scope", "src"], ["src/app.py", "--scope", "src"]]


def test_without_a_scope_nothing_changes(tmp_path):
    rec, world = _record(tmp_path, [OUT_OF_SCOPE, IN_SCOPE], None)
    assert "write_scope" not in rec
    assert [t["error_category"] for t in rec["turns"]] == ["NONE", "NONE", "NONE"]
    assert all(len(c[1]) == 1 for c in world.calls if c[0] == "write")


def test_a_scope_entry_new_at_base_is_a_digest(tmp_path):
    rec, _ = _record(tmp_path, [IN_SCOPE], ["src/app.py", "src/new.py"])
    assert rec["write_scope"] == ["src/app.py", path_digest("src/new.py")]


@pytest.mark.parametrize(
    "scope",
    [[], "src", ["../x"], ["src", 3], [{"sha256": "0" * 64, "bytes": 1}], ["./src"]],
)
def test_the_schema_checks_the_scope(tmp_path, scope):
    rec, _ = _record(tmp_path, [IN_SCOPE], ["src"])
    rec["write_scope"] = scope
    with pytest.raises(SchemaError):
        validate_record(rec)
    rec["write_scope"] = None  # null: no scope, like an absent key
    validate_record(rec)


def test_a_readable_scope_entry_needs_the_base_paths():
    with pytest.raises(SchemaError, match="write_scope"):
        _check_write_scope(["src"], plain_paths=False)
    _check_write_scope([path_digest("src")], plain_paths=False)


# -- the CLI ------------------------------------------------------------------------------------
@pytest.fixture
def harness(tmp_path, monkeypatch):
    return cli_harness(tmp_path, monkeypatch)


def test_cli_write_path_reaches_the_tools_the_manifest_and_the_line(harness):
    run, out = harness
    scope = ("--write-path", "src/", "--write-path", "src")
    assert run([OUT_OF_SCOPE, IN_SCOPE], "run", *scope) == 0
    (line,) = lines(out)
    assert line["write_scope"] == ["src"]
    assert [t["error_category"] for t in line["turns"]][:2] == ["REFUSED", "NONE"]
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["write_scope_rule"] == write_scope.WRITE_SCOPE_RULE
    private = out / "private" / line["episode_id"] / "transcript.json"
    assert json.loads(private.read_text())["write_scope"] == ["src"]
    assert run([], "run") == 2  # the frozen manifest: no unscoped episode in a scoped corpus


def test_cli_without_write_path_keeps_the_manifest_and_line(harness):
    run, out = harness
    assert run([OUT_OF_SCOPE], "run") == 0
    assert "write_scope" not in lines(out)[0]
    assert "write_scope_rule" not in json.loads((out / "manifest.json").read_text())


@pytest.mark.parametrize("bad", ["/abs", "../x", ".git"])
def test_cli_refuses_a_bad_write_path(harness, bad):
    run, out = harness
    assert run([], "run", "--write-path", bad) == 2
    assert not (out / "manifest.json").exists()


def test_a_resume_repeats_the_parents_scope(harness):
    run, out = harness
    scope = ("--write-path", "src")
    assert run([reply("read_file", path="src/app.py"), IN_SCOPE], "run", *scope) == 0
    tr = str(out / "private" / lines(out)[0]["episode_id"] / "transcript.json")
    done = [reply("finish", status="DONE")]
    other = ("--write-path", "src", "--write-path", "tests")
    assert run(done, "resume", "--parent-transcript", tr, *other) == 2
    assert run(done, "resume", "--parent-transcript", tr, *scope) == 0
    assert lines(out)[1]["write_scope"] == ["src"]
