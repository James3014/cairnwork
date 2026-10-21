"""Enforce Cairnwork size limits. Blocking: file <=400 lines, function <=80 lines, the trusted
sandbox package (src/cairnwork_sandbox) <=1200 lines of its own, src/cairnwork_verify <=600
lines. Warning only: core (src/cairnwork) above the 5000-line budget. Prints a per-package
table (also appended to $GITHUB_STEP_SUMMARY when set)."""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "src" / "cairnwork"
SANDBOX = ROOT / "src" / "cairnwork_sandbox"
VERIFY = ROOT / "src" / "cairnwork_verify"
SCANNED = [ROOT / "src", ROOT / "experimental", ROOT / "scripts", ROOT / "tests"]
MAX_FILE_LINES = 400
MAX_FUNCTION_LINES = 80
MAX_CORE_LINES = 5000  # warning budget, not a failure
MAX_SANDBOX_LINES = 1200
MAX_VERIFY_LINES = 600


def _python_files(base: Path) -> list[Path]:
    return sorted(p for p in base.rglob("*.py") if ".venv" not in p.parts)


def _top_package(path: Path, root: Path) -> str:
    head, *rest = path.relative_to(root).parts
    return rest[0] if head == "src" and len(rest) > 1 else head


def measure(root: Path = ROOT) -> tuple[list[str], list[str], dict[str, int]]:
    """Return (blocking problems, warnings, lines per package under src/ and experimental/)."""
    problems: list[str] = []
    warnings: list[str] = []
    totals: dict[str, int] = {}
    core_total = sandbox_total = verify_total = 0
    for base in SCANNED:
        for path in _python_files(base):
            text = path.read_text(encoding="utf-8")
            lines = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
            rel = path.relative_to(root)
            if rel.parts[0] in ("src", "experimental"):
                pkg = _top_package(path, root)
                totals[pkg] = totals.get(pkg, 0) + lines
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
        warnings.append(
            f"src/cairnwork total {core_total} lines > {MAX_CORE_LINES} warning budget: "
            "the PR must state reason, alternatives, responsibility, cost and verification"
        )
    if sandbox_total > MAX_SANDBOX_LINES:
        problems.append(f"src/cairnwork_sandbox total {sandbox_total} lines > {MAX_SANDBOX_LINES}")
    if verify_total > MAX_VERIFY_LINES:
        problems.append(f"src/cairnwork_verify total {verify_total} lines > {MAX_VERIFY_LINES}")
    return problems, warnings, totals


def check(root: Path = ROOT) -> list[str]:
    """Blocking problems only; the core-size warning is reported by measure()/main()."""
    return measure(root)[0]


def table(totals: dict[str, int]) -> str:
    src = {k: v for k, v in totals.items() if k.startswith("cairnwork")}
    rows = [("cairnwork (core)", src.get("cairnwork", 0), f"warn > {MAX_CORE_LINES}")]
    limits = {"cairnwork_sandbox": MAX_SANDBOX_LINES, "cairnwork_verify": MAX_VERIFY_LINES}
    for name in sorted(k for k in src if k != "cairnwork"):
        limit = limits.get(name)
        rows.append((name, src[name], f"max {limit}" if limit else "-"))
    rows.append(("experimental", totals.get("experimental", 0), "file/function limits only"))
    rows.append(("total formal source (src/)", sum(src.values()), "-"))
    width = max(len(r[0]) for r in rows)
    return "\n".join(f"{n:<{width}}  {c:>6}  {lim}" for n, c, lim in rows)


def main(root: Path | None = None) -> int:
    problems, warnings, totals = measure(root or ROOT)
    for problem in problems:
        print(problem)
    for warning in warnings:
        print(f"WARNING: {warning}")
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"::warning title=Core size budget::{warning}")
    text = table(totals)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"### Size table (lines)\n```\n{text}\n```\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
