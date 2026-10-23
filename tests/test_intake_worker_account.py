"""State config agy_run_as_user (Issue #183): validated like the other dispatcher keys."""

import json

import pytest

from cairnwork.intake import Intake, IntakeRefused


@pytest.fixture
def intake(tmp_path):
    made = Intake(tmp_path / "state")
    made.set_accepting(True)  # creates the state directory; config() reads config.json there
    return made


@pytest.mark.parametrize(
    "config",
    [
        {"agy_run_as_user": "_cairnwork_worker"},  # only the dispatcher can change account
        {"agy_dispatch": "/opt/pool/d", "agy_run_as_user": 5},
        {"agy_dispatch": "/opt/pool/d", "agy_run_as_user": ""},
        {"agy_dispatch": "/opt/pool/d", "agy_run_as_user": "a b"},
        {"agy_dispatch": "/opt/pool/d", "agy_run_as_user": "-u"},
        {"agy_dispatch": "/opt/pool/d", "agy_run_as_user": "x\n"},
        {"agy_dispatch": "/opt/pool/d", "agy_run_as_user": "a" * 65},
        {"agy_dispatch": "/opt/pool/d", "agy_run_as_user": True},
    ],
)
def test_bad_agy_run_as_user_settings_are_refused(intake, config):
    (intake.state / "config.json").write_text(json.dumps(config))
    with pytest.raises(IntakeRefused, match="BAD_CONFIG"):
        intake.config()


def test_agy_run_as_user_defaults_to_off_and_needs_the_dispatcher(intake):
    assert intake.config()["agy_run_as_user"] is None
    (intake.state / "config.json").write_text(
        json.dumps({"agy_dispatch": "/opt/pool/d", "agy_run_as_user": "_cairnwork_worker"})
    )
    assert intake.config()["agy_run_as_user"] == "_cairnwork_worker"
    (intake.state / "config.json").write_text(json.dumps({"agy_run_as_user": None}))
    assert intake.config()["agy_run_as_user"] is None
