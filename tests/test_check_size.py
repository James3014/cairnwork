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
