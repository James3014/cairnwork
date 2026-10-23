"""EXPERIMENTAL (default off, not imported by core): an on-demand BM25 file finder.

The worker asks for files when it needs them (``tool.py``); nothing is injected into its
prompt up front. ``find_files`` ranks files the query names first, then BM25 over content
and path tokens (``retrieval``). CLI: ``python -m experimental.bm25_hints find``. See
OWNER / EXPIRES.
"""

from .find import Hit, find_files
from .retrieval import rank_files
from .tool import FinderConfig, respond

__all__ = ["FinderConfig", "Hit", "find_files", "rank_files", "respond"]
