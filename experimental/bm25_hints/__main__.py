"""``python -m experimental.bm25_hints <repo> <issue-file> [-k N]`` prints a JSON list."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .retrieval import rank_files


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m experimental.bm25_hints")
    ap.add_argument("repo", type=Path)
    ap.add_argument("issue_file", type=Path)
    ap.add_argument("-k", type=int, default=10)
    ap.add_argument("--include-tests", action="store_true")
    args = ap.parse_args(argv)
    text = args.issue_file.read_text(encoding="utf-8", errors="replace")
    print(json.dumps(rank_files(args.repo, text, args.k, args.include_tests), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
