"""Exact-base Pyright gate: fail only on findings the PR adds relative to its merge base.

Usage: pyright_gate.py BASE.json BASE_ROOT HEAD.json HEAD_ROOT   (pyright --outputjson files)

A finding is identified by (relative file, rule, message, stripped source line), never by line
number, so inserting lines above old debt does not resurface it. Counts are compared per key, so
a second copy of an old finding is new. Exit 0 = nothing new, 1 = new findings, 2 = unreadable
input or changed or extending pyright configuration (fail closed).

Fails closed (2) when either report analyzed no files, and when pyrightconfig.json or the
[tool.pyright] table of pyproject.toml differs between the two roots: a config change needs a
human, not a silent comparison.

Known limitations: a renamed file's findings all show as new (fails closed); fixing one finding
and adding an identical-text one elsewhere in the same file is not detected.
A file-level `# type: ignore` or a new stub (.pyi / typings/) can hide findings; that is
visible in the diff and left to reviewers.
"""

from __future__ import annotations

import json
import sys
import tomllib
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
        if not data["summary"]["filesAnalyzed"]:
            raise ValueError("pyright analyzed no files")
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


def _config(root: Path) -> tuple[str | None, object]:
    cfg = root / "pyrightconfig.json"
    text = cfg.read_text(encoding="utf-8") if cfg.is_file() else None
    try:
        table = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except OSError:
        return text, None
    return text, table.get("tool", {}).get("pyright")


def _extends(text: str | None, table: object) -> bool:
    # pyrightconfig.json may carry comments, so match the key textually rather than parse it.
    return bool(text and '"extends"' in text) or (isinstance(table, dict) and "extends" in table)


def new_findings(base: Counter[Key], head: Counter[Key]) -> list[Key]:
    extra = head - base
    return sorted(extra.elements())


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__)
        return 2
    try:
        configs = [_config(Path(argv[1])), _config(Path(argv[3]))]
        if any(_extends(*config) for config in configs):
            raise GateError("pyright config uses extends; a human must review")
        if configs[0] != configs[1]:
            raise GateError("pyright configuration changed; a human must review this PR")
        base = load(Path(argv[0]), Path(argv[1]).resolve())
        head = load(Path(argv[2]), Path(argv[3]).resolve())
    except (GateError, OSError, ValueError) as exc:
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
