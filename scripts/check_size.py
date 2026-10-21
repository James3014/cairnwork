"""Enforce Cairnwork size limits: file <=400 lines, function <=80 lines, core <=5000 lines,
the trusted sandbox package (src/cairnwork_sandbox) <=1200 lines of its own, and
src/cairnwork_verify <=600 lines."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "src" / "cairnwork"
SANDBOX = ROOT / "src" / "cairnwork_sandbox"
VERIFY = ROOT / "src" / "cairnwork_verify"
SCANNED = [ROOT / "src", ROOT / "experimental", ROOT / "scripts", ROOT / "tests"]
MAX_FILE_LINES = 400
MAX_FUNCTION_LINES = 80
MAX_CORE_LINES = 5000
MAX_SANDBOX_LINES = 1200
MAX_VERIFY_LINES = 600


def _python_files(base: Path) -> list[Path]:
    return sorted(p for p in base.rglob("*.py") if ".venv" not in p.parts)


def check(root: Path = ROOT) -> list[str]:
    problems: list[str] = []
    core_total = sandbox_total = verify_total = 0
    for base in SCANNED:
        for path in _python_files(base):
            text = path.read_text(encoding="utf-8")
            lines = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
            rel = path.relative_to(root)
            if lines > MAX_FILE_LINES:
                problems.append(f"{rel}: {lines} lines > {MAX_FILE_LINES}")
            if CORE in path.parents:
                core_total += lines
            if SANDBOX in path.parents:
                sandbox_total += lines
            if VERIFY in path.parents:
                verify_total += lines
            for node in ast.walk(ast.parse(text, filename=str(path))):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    length = (node.end_lineno or node.lineno) - node.lineno + 1
                    if length > MAX_FUNCTION_LINES:
                        where = f"{rel}:{node.lineno} {node.name}"
                        problems.append(f"{where}: {length} lines > {MAX_FUNCTION_LINES}")
    if core_total > MAX_CORE_LINES:
        problems.append(f"src/cairnwork total {core_total} lines > {MAX_CORE_LINES}")
    if sandbox_total > MAX_SANDBOX_LINES:
        problems.append(f"src/cairnwork_sandbox total {sandbox_total} lines > {MAX_SANDBOX_LINES}")
    if verify_total > MAX_VERIFY_LINES:
        problems.append(f"src/cairnwork_verify total {verify_total} lines > {MAX_VERIFY_LINES}")
    return problems


def main() -> int:
    problems = check()
    for problem in problems:
        print(problem)
    core = sum(p.read_text(encoding="utf-8").count("\n") for p in _python_files(CORE))
    print(f"core: {core}/{MAX_CORE_LINES} lines (packages outside src/cairnwork are not counted)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
