"""EXPERIMENTAL (default off, not imported by core): BM25 file hints for an Issue.

Given a checkout at the base commit and the Issue text, ``rank_files`` returns the
repo-relative paths most likely to need a change. See OWNER / EXPIRES.
"""

from .retrieval import rank_files

__all__ = ["rank_files"]
