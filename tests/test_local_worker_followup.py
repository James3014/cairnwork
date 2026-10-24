"""Round-2 review of #239: the wall-time rule in the manifest (NB9), a naive canary finish time
(NB10), the two test gaps X12 and X18, and the action lists pinned to the tools the loop offers."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from local_worker_fakes import META, TASK, canary_checks, cli_harness, make_loop, reply

from experimental.local_worker import canary, corpus_io, cuts, episode, schema_rules
from experimental.local_worker.agent import initial_messages
from experimental.local_worker.agent_protocol import TOOL_SCHEMAS, TOOLS_ABSENT
from experimental.local_worker.episode_schema import SchemaError, validate_record


def test_manifest_states_the_wall_time_rule(tmp_path, monkeypatch):
    run, out = cli_harness(tmp_path, monkeypatch)
    assert run([reply("read_file", path="src/app.py")], "run") == 0
    manifest = json.loads((out / "manifest.json").read_text())
    rule = manifest["wall_time_rule"]
    assert rule == episode.WALL_TIME_RULE
    assert "L0 run's wall_clock_seconds + each resume run's own_wall_seconds" in rule
    assert {"null_rule", "tokens_accum_formula", "cut_definition"} <= set(manifest)


def _canary(tmp_path, **changes: Any):
    wrapper = tmp_path / "local-worker-tool"
    wrapper.write_bytes(corpus_io.REPO_WRAPPER.read_bytes())
    report = {
        "schema": canary.SCHEMA,
        "checks": canary_checks(),
        "overall": "PASS",
        "mode": "worker",
        "wrapper": str(wrapper),
        "wrapper_sha256": hashlib.sha256(wrapper.read_bytes()).hexdigest(),
        "host": "mini",
        "finished_at": datetime.now(UTC).isoformat(),
        **changes,
    }
    data = json.dumps(report).encode()
    path = tmp_path / "out" / "canary.json"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    Path(str(path) + ".sha256").write_text(hashlib.sha256(data).hexdigest() + "  x\n")
    return lambda: corpus_io.verify_canary(path, tmp_path / "out", wrapper=wrapper, host="mini")


@pytest.mark.parametrize(
    "finished_at",
    [datetime.now().isoformat(), "2026-10-24T10:00:00", 1761300000, None, "yesterday"],
)
def test_a_canary_finish_time_without_an_offset_is_a_clean_refusal(tmp_path, finished_at):
    verify = _canary(tmp_path, finished_at=finished_at)
    with pytest.raises(corpus_io.InputRefused, match="finished_at"):
        verify()


def test_x12_a_probe_row_without_pass_is_refused(tmp_path):
    checks = canary_checks()
    rows = checks["b_write_denied"]["probes"]
    checks["b_write_denied"]["probes"] = [{"probe": rows[0]["probe"]}, *rows[1:]]
    with pytest.raises(corpus_io.InputRefused, match="did not pass"):
        _canary(tmp_path, checks=checks)()


def test_x18_a_source_cut_after_an_unobserved_turn_is_refused(tmp_path):
    replies = [
        reply("write_file", path="tests/test_x.py", content="def test_x(): 0\n"),
        reply("run_tests"),
        reply("edit_file", path="src/app.py", target="x = 1", replacement="x = 2"),
    ]
    loop, _, _, _ = make_loop(tmp_path, replies)
    rec = episode.build_record(META, loop.run(initial_messages(TASK)))
    validate_record(rec)
    assert rec["first_edit_cut"]["cut_turn_index"] == 0
    assert rec["first_source_edit"]["turn_index"] == 2
    rec["turns"][1]["tree_differs_from_base"] = None  # run_tests ran; its tree never observed
    with pytest.raises(SchemaError, match="first_source_edit is not a tree change"):
        validate_record(rec)


def test_the_action_lists_match_the_tools_the_loop_offers():
    """1ca5/nori: READ_ACTIONS named a "search" action no tool offers and the schema refuses."""
    offered = {t["function"]["name"] for t in TOOL_SCHEMAS}
    actions = {"run_test" if n == "run_tests" else n for n in offered}  # the spec's action name
    assert set(schema_rules.ACTION_TYPES) == actions | {"invalid_tool_call", "no_tool_call"}
    assert set(cuts.READ_ACTIONS) == {"read_file", "list_dir"} <= actions
    assert not set(TOOLS_ABSENT) & (set(cuts.READ_ACTIONS) | set(schema_rules.ACTION_TYPES))
