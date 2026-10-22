"""`cairnwork-route submit` refuses a replayed request with a code and exit 2."""

import pytest
from test_route import project, request, route, state, wait_finished  # noqa: F401 - fixtures

pytestmark = pytest.mark.sandbox


def test_submit_of_a_finished_request_under_a_new_key_is_forbidden(state, project):  # noqa: F811
    route(state, "accept")
    code, first = route(state, "submit", stdin=request(project))
    assert code == 0
    wait_finished(state, first["task_id"])
    code, out = route(state, "submit", stdin=request(project, key="gw-2"))
    assert code == 2 and out["error"] == "REPLAY_FORBIDDEN"
    assert route(state, "in-flight")[1]["count"] == 0
