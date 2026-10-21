from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import pyright_gate as gate


def _tree(root: Path, source: str) -> Path:
    root.mkdir()
    (root / "m.py").write_text(source, encoding="utf-8")
    return root


def _report(
    root: Path,
    findings: list[tuple[int, str]],
    name: str = "r.json",
    severity: str = "error",
    rule: str = "reportGeneralTypeIssues",
    files: int = 1,
) -> Path:
    diagnostics = [
        {
            "file": str(root / "m.py"),
            "severity": severity,
            "message": message,
            "range": {"start": {"line": line, "character": 0}},
            "rule": rule,
        }
        for line, message in findings
    ]
    path = root.parent / f"{root.name}-{name}"
    data = {"generalDiagnostics": diagnostics, "summary": {"filesAnalyzed": files}}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _run(base: Path, base_report: Path, head: Path, head_report: Path) -> int:
    return gate.main([str(base_report), str(base), str(head_report), str(head)])


def test_line_shift_is_not_new(tmp_path: Path) -> None:
    base = _tree(tmp_path / "base", "x: int = 'a'\n")
    head = _tree(tmp_path / "head", "import os\n\nx: int = 'a'\n")
    code = _run(base, _report(base, [(0, "bad")]), head, _report(head, [(2, "bad")]))
    assert code == 0


def test_edited_line_text_is_new(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = _tree(tmp_path / "base", "x: int = 'a'\n")
    head = _tree(tmp_path / "head", "x: int = 'b'\n")
    code = _run(base, _report(base, [(0, "bad")]), head, _report(head, [(0, "bad")]))
    assert code == 1
    assert "NEW m.py" in capsys.readouterr().out


def test_duplicate_of_old_finding_is_new(tmp_path: Path) -> None:
    base = _tree(tmp_path / "base", "x: int = 'a'\n")
    head = _tree(tmp_path / "head", "x: int = 'a'\nx: int = 'a'\n")
    code = _run(base, _report(base, [(0, "bad")]), head, _report(head, [(0, "bad"), (1, "bad")]))
    assert code == 1


def test_removed_finding_is_fine(tmp_path: Path) -> None:
    base = _tree(tmp_path / "base", "x: int = 'a'\n")
    head = _tree(tmp_path / "head", "x: int = 1\n")
    assert _run(base, _report(base, [(0, "bad")]), head, _report(head, [])) == 0


def test_malformed_json_fails_closed(tmp_path: Path) -> None:
    base = _tree(tmp_path / "base", "x = 1\n")
    head = _tree(tmp_path / "head", "x = 1\n")
    good = _report(head, [])
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert _run(base, bad, head, good) == 2
    assert _run(base, tmp_path / "missing.json", head, good) == 2
    bad.write_text(json.dumps({"generalDiagnostics": []}), encoding="utf-8")
    assert _run(base, bad, head, good) == 2


def test_zero_files_analyzed_fails_closed(tmp_path: Path) -> None:
    base = _tree(tmp_path / "base", "x = 1\n")
    head = _tree(tmp_path / "head", "x = 1\n")
    empty = _report(head, [], name="empty.json", files=0)
    assert _run(base, _report(base, []), head, empty) == 2
    assert _run(base, empty, head, _report(head, [])) == 2


def test_information_severity_is_ignored(tmp_path: Path) -> None:
    base = _tree(tmp_path / "base", "x = 1\n")
    head = _tree(tmp_path / "head", "x = 1\n")
    info = _report(head, [(0, "hint")], severity="information")
    assert _run(base, _report(base, []), head, info) == 0


def test_rule_is_part_of_the_key(tmp_path: Path) -> None:
    base = _tree(tmp_path / "base", "x: int = 'a'\n")
    head = _tree(tmp_path / "head", "x: int = 'a'\n")
    old = _report(base, [(0, "bad")], rule="reportA")
    assert _run(base, old, head, _report(head, [(0, "bad")], rule="reportB")) == 1


def test_message_is_part_of_the_key(tmp_path: Path) -> None:
    base = _tree(tmp_path / "base", "x: int = 'a'\n")
    head = _tree(tmp_path / "head", "x: int = 'a'\n")
    assert _run(base, _report(base, [(0, "old")]), head, _report(head, [(0, "new")])) == 1


def test_config_change_fails_closed(tmp_path: Path) -> None:
    base = _tree(tmp_path / "base", "x = 1\n")
    head = _tree(tmp_path / "head", "x = 1\n")
    (head / "pyrightconfig.json").write_text("{}", encoding="utf-8")
    assert _run(base, _report(base, []), head, _report(head, [])) == 2


def test_pyproject_pyright_table_change_fails_closed(tmp_path: Path) -> None:
    base = _tree(tmp_path / "base", "x = 1\n")
    head = _tree(tmp_path / "head", "x = 1\n")
    (base / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    (head / "pyproject.toml").write_text("[tool.pyright]\nstrict = ['.']\n", encoding="utf-8")
    assert _run(base, _report(base, []), head, _report(head, [])) == 2
    (head / "pyproject.toml").write_text("[tool.ruff]\nline-length = 1\n", encoding="utf-8")
    assert _run(base, _report(base, []), head, _report(head, [])) == 0


def test_extends_in_either_config_fails_closed(tmp_path: Path) -> None:
    base = _tree(tmp_path / "base", "x = 1\n")
    head = _tree(tmp_path / "head", "x = 1\n")
    for side in (base, head):
        for other in (base, head):
            (other / "pyrightconfig.json").unlink(missing_ok=True)
            (other / "pyproject.toml").unlink(missing_ok=True)
        (side / "pyrightconfig.json").write_text('{"extends": "./x.json"}', encoding="utf-8")
        assert _run(base, _report(base, []), head, _report(head, [])) == 2
        (side / "pyrightconfig.json").unlink()
        (side / "pyproject.toml").write_text('[tool.pyright]\nextends = "x.json"\n')
        assert _run(base, _report(base, []), head, _report(head, [])) == 2


def test_identical_extends_on_both_sides_still_fails_closed(tmp_path: Path) -> None:
    base = _tree(tmp_path / "base", "x = 1\n")
    head = _tree(tmp_path / "head", "x = 1\n")
    for root in (base, head):
        (root / "pyrightconfig.json").write_text('{"extends": "./x.json"}', encoding="utf-8")
    assert _run(base, _report(base, []), head, _report(head, [])) == 2
    for root in (base, head):
        (root / "pyrightconfig.json").unlink()
        (root / "pyproject.toml").write_text('[tool.pyright]\nextends = "x.json"\n')
    assert _run(base, _report(base, []), head, _report(head, [])) == 2


def test_missing_generaldiagnostics_fails_closed(tmp_path: Path) -> None:
    base = _tree(tmp_path / "base", "x = 1\n")
    head = _tree(tmp_path / "head", "x = 1\n")
    bad = tmp_path / "nodiag.json"
    bad.write_text(json.dumps({"summary": {"filesAnalyzed": 3}}), encoding="utf-8")
    assert _run(base, bad, head, _report(head, [])) == 2
