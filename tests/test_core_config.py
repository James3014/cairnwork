import json

import pytest
from core_http_server import core_http_server

from cairnwork.intake import Intake, IntakeRefused


def _intake(tmp_path, config):
    intake = Intake(tmp_path / "state")
    intake.prepare()
    (intake.state / "config.json").write_text(json.dumps(config))
    return intake


@pytest.mark.parametrize(
    "config",
    [
        {"core_url": "http://10.0.0.5:8080", "core_token_file": "/x/token"},
        {"core_url": "http://127.0.0.1:8080"},  # no token file
        {"core_url": "http://127.0.0.1:8080", "core_token_file": "relative/token"},
    ],
)
def test_bad_core_settings_are_refused(tmp_path, config):
    with pytest.raises(IntakeRefused, match="BAD_CONFIG"):
        _intake(tmp_path, config).config()


def test_without_core_url_the_notary_calls_core_in_process(tmp_path):
    assert _intake(tmp_path, {}).notary._core_verify.__module__ != "cairnwork_protocol.http"


def test_with_core_url_the_notary_calls_core_over_http(tmp_path):
    with core_http_server(tmp_path) as (url, token_file):
        intake = _intake(tmp_path, {"core_url": url, "core_token_file": str(token_file)})
        notary = intake.notary
        assert notary._core_verify.__module__ == "cairnwork_protocol.http"
        status, body = notary._core_verify({})
    assert status == 422 and "error" in body  # Core's own refusal of an empty request
