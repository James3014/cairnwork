from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci import pyright_trusted_status as status

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = (ROOT / ".github" / "workflows" / "pyright-trusted.yml").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("job", "rc", "state"),
    [
        ("success", "0", "success"),
        ("success", "1", "failure"),
        ("success", "2", "error"),
        ("success", "3", "error"),
        ("success", "", "error"),
        ("success", "garbage", "error"),
        ("failure", "0", "error"),
        ("cancelled", "0", "error"),
        ("skipped", "", "error"),
        ("", "0", "error"),
    ],
)
def test_only_clean_success_passes(job: str, rc: str, state: str) -> None:
    assert status.decide(job, rc) == state


def test_description_is_fixed_text(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("PYRIGHT_JOB_RESULT", "success\nstate=success")
    monkeypatch.setenv("PYRIGHT_GATE_RC", "0")
    status.main()
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "state=error"
    assert lines[1] == f"description={status.DESCRIPTIONS['error']}"


def test_workflow_trust_boundary() -> None:
    assert "pull_request_target:" in WORKFLOW
    assert WORKFLOW.count("persist-credentials: false") == 3
    # Only the publish job may write statuses; the job that reads PR content may not.
    analyze, publish = WORKFLOW.split("  publish:")
    assert "statuses: write" not in analyze
    assert "statuses: write" in publish
    assert "head.sha" in analyze
    assert "uv sync" not in WORKFLOW  # dependency install happens inside the trusted script
    assert "run: python3 scripts/ci/pyright_trusted_status.py" in publish
    assert "${{ github.event.pull_request.title" not in WORKFLOW
