import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location("check_size", ROOT / "scripts" / "check_size.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_repository_is_within_size_limits():
    assert _load().check() == []


def test_long_function_is_reported(tmp_path, monkeypatch):
    module = _load()
    src = tmp_path / "src" / "cairnwork"
    src.mkdir(parents=True)
    body = "\n".join("    x = 1" for _ in range(module.MAX_FUNCTION_LINES))
    (src / "long.py").write_text(f"def too_long():\n{body}\n", encoding="utf-8")
    monkeypatch.setattr(module, "CORE", src)
    monkeypatch.setattr(module, "SCANNED", [tmp_path / "src"])
    problems = module.check(tmp_path)
    assert any("too_long" in p for p in problems)


def test_verify_package_total_is_capped(tmp_path, monkeypatch):
    module = _load()
    pkg = tmp_path / "src" / "cairnwork_verify"
    pkg.mkdir(parents=True)
    for index in range(3):
        (pkg / f"m{index}.py").write_text("x = 1\n" * 250, encoding="utf-8")
    monkeypatch.setattr(module, "VERIFY", pkg)
    monkeypatch.setattr(module, "SCANNED", [tmp_path / "src"])
    assert any("cairnwork_verify total" in p for p in module.check(tmp_path))


def _tree(tmp_path, monkeypatch, module, packages):
    """Build a temp src/ tree ({package: [lines per file]}) and point the checker at it."""
    for pkg, sizes in packages.items():
        base = tmp_path / "src" / pkg
        base.mkdir(parents=True)
        for index, size in enumerate(sizes):
            (base / f"m{index}.py").write_text("x = 1\n" * size, encoding="utf-8")
    monkeypatch.setattr(module, "CORE", tmp_path / "src" / "cairnwork")
    monkeypatch.setattr(module, "SANDBOX", tmp_path / "src" / "cairnwork_sandbox")
    monkeypatch.setattr(module, "VERIFY", tmp_path / "src" / "cairnwork_verify")
    monkeypatch.setattr(module, "SCANNED", [tmp_path / "src"])
    monkeypatch.setattr(module, "ROOT", tmp_path)


def test_core_over_budget_warns_but_exits_zero(tmp_path, monkeypatch, capsys):
    module = _load()
    _tree(tmp_path, monkeypatch, module, {"cairnwork": [300] * 17})
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert module.check(tmp_path) == []
    assert module.main(tmp_path) == 0
    out = capsys.readouterr().out
    assert "WARNING: src/cairnwork total 5100" in out
    assert "::warning" not in out


def test_warning_annotation_and_summary_in_github_actions(tmp_path, monkeypatch, capsys):
    module = _load()
    _tree(tmp_path, monkeypatch, module, {"cairnwork": [300] * 17})
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert module.main(tmp_path) == 0
    assert "::warning" in capsys.readouterr().out
    assert "cairnwork (core)" in summary.read_text(encoding="utf-8")


def test_core_under_budget_has_no_warning(tmp_path, monkeypatch, capsys):
    module = _load()
    _tree(tmp_path, monkeypatch, module, {"cairnwork": [300] * 3})
    assert module.main(tmp_path) == 0
    assert "WARNING" not in capsys.readouterr().out


def test_file_over_400_lines_still_blocks(tmp_path, monkeypatch, capsys):
    module = _load()
    _tree(tmp_path, monkeypatch, module, {"cairnwork": [401]})
    assert any("401 lines > 400" in p for p in module.check(tmp_path))
    assert module.main(tmp_path) == 1


def test_file_of_exactly_400_lines_passes(tmp_path, monkeypatch):
    module = _load()
    _tree(tmp_path, monkeypatch, module, {"cairnwork": [400]})
    assert module.main(tmp_path) == 0


def test_function_of_81_lines_still_blocks(tmp_path, monkeypatch):
    module = _load()
    _tree(tmp_path, monkeypatch, module, {"cairnwork": []})
    body = "\n".join("    x = 1" for _ in range(80))
    (tmp_path / "src" / "cairnwork" / "f.py").write_text(f"def f():\n{body}\n", encoding="utf-8")
    assert any("f: 81 lines > 80" in p for p in module.check(tmp_path))
    assert module.main(tmp_path) == 1


def test_over_limit_package_blocks_even_with_core_warning(tmp_path, monkeypatch, capsys):
    module = _load()
    _tree(
        tmp_path,
        monkeypatch,
        module,
        {"cairnwork": [300] * 17, "cairnwork_sandbox": [300] * 5, "cairnwork_verify": [300] * 3},
    )
    problems = module.check(tmp_path)
    assert any("cairnwork_sandbox total 1500" in p for p in problems)
    assert any("cairnwork_verify total 900" in p for p in problems)
    assert module.main(tmp_path) == 1
    assert "WARNING: src/cairnwork total" in capsys.readouterr().out


def test_oversized_file_blocks_even_with_core_warning(tmp_path, monkeypatch, capsys):
    module = _load()
    _tree(tmp_path, monkeypatch, module, {"cairnwork": [300] * 17 + [401]})
    assert any("401 lines > 400" in p for p in module.check(tmp_path))
    assert module.main(tmp_path) == 1
    assert "WARNING: src/cairnwork total" in capsys.readouterr().out


def test_table_lists_every_package_and_total(tmp_path, monkeypatch, capsys):
    module = _load()
    _tree(
        tmp_path,
        monkeypatch,
        module,
        {"cairnwork": [10], "cairnwork_protocol": [20], "cairnwork_sandbox": [30]},
    )
    assert module.main(tmp_path) == 0
    out = capsys.readouterr().out
    for label in ("cairnwork (core)", "cairnwork_protocol", "cairnwork_sandbox", "experimental"):
        assert label in out
    assert "total formal source (src/)" in out
    assert "60" in out.splitlines()[-1]
