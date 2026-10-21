"""Exact-base Pyright gate: fail only on findings the PR adds relative to its merge base.

Usage: pyright_gate.py BASE.json BASE_ROOT HEAD.json HEAD_ROOT   (pyright --outputjson files)

A finding is identified by (relative file, rule, message, stripped source line), never by line
number, so inserting lines above old debt does not resurface it. Counts are compared per key, so
a second copy of an old finding is new. Exit 0 = nothing new, 1 = new findings, 2 = unreadable
input (fail closed).
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

Key = tuple[str, str, str, str]


class GateError(Exception):
    pass


def _source_line(path: Path, line: int) -> str:
    try:
        return path.read_text(encoding="utf-8").splitlines()[line].strip()
    except (OSError, UnicodeDecodeError, IndexError):
        return ""


def load(report: Path, root: Path) -> Counter[Key]:
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
        diagnostics = data["generalDiagnostics"]
        found: Counter[Key] = Counter()
        for item in diagnostics:
            if item["severity"] not in ("error", "warning"):
                continue
            path = Path(item["file"])
            rel = path.relative_to(root).as_posix() if path.is_absolute() else path.as_posix()
            text = _source_line(root / rel, item["range"]["start"]["line"])
            found[(rel, item.get("rule", ""), item["message"], text)] += 1
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise GateError(f"cannot read pyright report {report}: {exc!r}") from exc
    return found


def new_findings(base: Counter[Key], head: Counter[Key]) -> list[Key]:
    extra = head - base
    return sorted(extra.elements())


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__)
        return 2
    try:
        base = load(Path(argv[0]), Path(argv[1]).resolve())
        head = load(Path(argv[2]), Path(argv[3]).resolve())
    except GateError as exc:
        print(f"pyright gate failed closed: {exc}")
        return 2
    added = new_findings(base, head)
    print(f"pyright: base {sum(base.values())} findings, head {sum(head.values())} findings")
    for rel, rule, message, text in added:
        print(f"NEW {rel} [{rule}] {message}\n    {text}")
    if added:
        print(f"{len(added)} new pyright finding(s) introduced by this change")
        return 1
    print("no new pyright findings")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
