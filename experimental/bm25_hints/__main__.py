"""``python -m experimental.bm25_hints find --repo R --query TEXT [-k N]`` prints JSON.

``--commit C`` reads the files of commit C from R's object store (the form a worker-facing
caller uses: the worker's clone is never touched); without it R's tracked working-tree
files are read. ``--query-file`` reads the query from a file instead. Output::

    {"query": "...", "results": [{"path": "...", "score": 1.23, "reason": "bm25"}, ...]}

``reason`` is ``named`` for a file the query names (listed first, score 0) or ``bm25``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .commit_source import commit_texts
from .find import find_files
from .retrieval import checkout_texts


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m experimental.bm25_hints")
    sub = ap.add_subparsers(dest="command", required=True)
    find = sub.add_parser("find", help="rank a repository's files against a query")
    find.add_argument("--repo", type=Path, required=True)
    query = find.add_mutually_exclusive_group(required=True)
    query.add_argument("--query")
    query.add_argument("--query-file", type=Path)
    find.add_argument("--commit", help="read this commit's files instead of the working tree")
    find.add_argument("-k", type=int, default=5)
    find.add_argument("--include-tests", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    text = args.query
    if args.query_file is not None:
        text = args.query_file.read_text(encoding="utf-8", errors="replace")
    items = commit_texts(args.repo, args.commit) if args.commit else checkout_texts(args.repo)
    hits = find_files(items, text, args.k, args.include_tests)
    print(json.dumps({"query": text, "results": [h.as_json() for h in hits]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
