"""On-demand file finder: the files of a repository most likely relevant to a query.

Ranking (the "d4" zero-cost locator of the 2026-10-22 L2 council, without the git-recency
term that cost 2 points there): every tracked file the query names by path or file name
comes first, in the order the query names them, then the rest by BM25 (``retrieval``).
On the time-held-out set it reached top-5 hit 73% overall, 89% when the request named a
path and 6% when it did not (knowledge ``nexus/decisions/council-2026-10-22-l2-breakthrough
/r7_offline``). Pushing a hint list into the prompt up front saved a median ~0.3% of tokens,
so this is offered only as a tool the worker asks for (``tool.py``), never injected.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from .retrieval import score_texts

# A path with at least one '/', or a bare file name with a common source/config extension.
_PATH_TOKEN = re.compile(
    r"(?<![\w@])((?:~|/|\.{1,2}/)?[\w.\-]+(?:/[\w.\-]+)+(?:\.[A-Za-z0-9]{1,8})?"
    r"|[\w\-]+\.(?:py|pyi|md|rst|sh|json|yaml|yml|toml|txt|ts|js|tsx|jsx|rs|go|java|c|h|cc"
    r"|cpp|hpp|rb|html|css|cfg|ini|csv|tsv))(?![\w/])"
)
MAX_MENTIONED = 50
MAX_K = 50


@dataclass(frozen=True)
class Hit:
    path: str
    score: float  # BM25 score; 0.0 for a file that is listed because the query names it
    reason: str  # "named" (the query names this file) or "bm25"

    def as_json(self) -> dict:
        return {"path": self.path, "score": round(self.score, 4), "reason": self.reason}


def named_files(query: str, files: Iterable[str]) -> list[str]:
    """Tracked files the query names: an exact path, a path suffix, or a bare file name."""
    pool = list(files)
    hits: list[str] = []
    for token in _PATH_TOKEN.findall(query):
        name = token.strip("./~")
        if not name:
            continue
        base = name.rsplit("/", 1)[-1]
        for f in pool:
            if f in hits:
                continue
            if (
                f == name
                or f.endswith("/" + name)
                or ("/" not in name and f.rsplit("/", 1)[-1] == base)
            ):
                hits.append(f)
                if len(hits) >= MAX_MENTIONED:
                    return hits
    return hits


def find_files(
    items: Iterable[tuple[str, str]], query: str, k: int = 5, include_tests: bool = False
) -> list[Hit]:
    """The ``k`` (capped at MAX_K) files of ``items`` (``(path, text)``) most relevant to ``query``.

    A file named in the query is listed even if it is a test file: the caller asked for it.
    """
    k = max(0, min(k, MAX_K))
    texts = list(items)
    named = named_files(query, (p for p, _ in texts))
    out = [Hit(p, 0.0, "named") for p in named]
    seen = set(named)
    for path, score in score_texts(texts, query, include_tests):
        if len(out) >= k:
            break
        if path in seen:
            continue
        out.append(Hit(path, score, "bm25"))
    return out[:k]
