"""Tokeniser and a small Okapi BM25 index (pure stdlib)."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
_MIN_PART = 3


def tokens(text: str) -> list[str]:
    """Lower-cased identifiers, plus their snake_case/CamelCase parts (length >= 3)."""
    out: list[str] = []
    for word in _TOKEN_RE.findall(text):
        whole = word.lower()
        out.append(whole)
        parts = [
            p.lower()
            for piece in word.split("_")
            for p in _CAMEL_RE.findall(piece)
            if len(p) >= _MIN_PART
        ]
        if len(parts) > 1 or (parts and parts[0] != whole):
            out.extend(parts)
    return out


class BM25Index:
    """Okapi BM25 over pre-tokenised documents; document order is the tie-break basis."""

    def __init__(self, docs: Iterable[list[str]], k1: float = 1.2, b: float = 0.75) -> None:
        self._tf = [Counter(doc) for doc in docs]
        self._len = [sum(c.values()) for c in self._tf]
        self.k1, self.b = k1, b
        n = len(self._tf)
        self._avg = sum(self._len) / n if n else 0.0
        df: Counter[str] = Counter()
        self._postings: dict[str, list[int]] = {}
        for i, counts in enumerate(self._tf):
            for term in counts:
                df[term] += 1
                self._postings.setdefault(term, []).append(i)
        self._idf = {t: math.log((n - c + 0.5) / (c + 0.5) + 1.0) for t, c in df.items()}

    def scores(self, query: list[str]) -> dict[int, float]:
        """Score of every document sharing at least one query term (positive scores only)."""
        if self._avg <= 0:
            return {}
        totals: dict[int, float] = {}
        for term, qf in Counter(query).items():
            idf = self._idf.get(term)
            if idf is None:
                continue
            for i in self._postings[term]:
                f = self._tf[i][term]
                norm = f + self.k1 * (1 - self.b + self.b * self._len[i] / self._avg)
                totals[i] = totals.get(i, 0.0) + qf * idf * f * (self.k1 + 1) / norm
        return {i: s for i, s in totals.items() if s > 0}
