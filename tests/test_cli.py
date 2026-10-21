"""The quickstart CLI: ``init`` writes a usable state, and ``demo`` runs the whole governed
verification flow on the self-contained fixture and reaches a Core VERIFIED verdict."""

import json

import pytest

from cairnwork_quickstart.cli import main


def test_init_writes_a_state_config(tmp_path, capsys):
    state = tmp_path / "state"
    args = ["init", "--dir", str(state), "--no-verifier-account", "--verifier-python", "/x"]
    assert main(args) == 0
    config = json.loads((state / "config.json").read_text())
    assert config == {"verifier_account": False, "verifier_python": "/x"}
    assert (state / "ACCEPTING").exists()
    assert oct((state).stat().st_mode)[-3:] == "700"


@pytest.mark.sandbox
def test_demo_reaches_a_core_verified_verdict(capsys):
    # End to end: build the fixture, run verify_candidate under the real sandbox, Core VERIFIED.
    assert main(["demo"]) == 0
    out = capsys.readouterr().out
    assert "VERIFIED" in out and "core status:  VERIFIED" in out
