"""The worker-facing seam of the file finder (experimental, default off).

Pull, not push: the worker gets a short tool description and asks when it wants files; it is
never handed a hint list up front (that saved a median ~0.3% of tokens in the 2026-10-22 L2
council's offline replay, see ``find.py``).

The Agy worker runs headless and cannot run commands or call tools, so a request travels as
text: the worker ends its reply with up to ``MAX_REQUESTS`` lines ``CAIRNWORK_FIND_FILES:
<what you are looking for>``. A turn wrapper of the kind Issue #32 adds for visible tests
(run the inner worker, look at what it produced, run it again with feedback appended) reads
the reply, calls ``respond`` and gives the worker one more turn with the answer appended to
its instructions. Until such a wrapper (or a native tool loop) exists nothing calls this
module; core never imports it.

Everything here is off unless ``FinderConfig.enabled`` is set (``from_env``:
``CAIRNWORK_EXPERIMENTAL_FILE_FINDER=1``). Requests are answered from Cairnwork's own
repository at the task's base commit (``commit_source``), never from the worker's clone.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .commit_source import commit_texts
from .find import FileFinder

ENV_FLAG = "CAIRNWORK_EXPERIMENTAL_FILE_FINDER"
REQUEST_PREFIX = "CAIRNWORK_FIND_FILES:"
MAX_REQUESTS = 3
MAX_QUERY_CHARS = 500
MAX_PATH_CHARS = 200
_REQUEST = re.compile(r"^[ \t>*`-]*" + re.escape(REQUEST_PREFIX) + r"[ \t]*(.+?)[ \t`]*$", re.M)
# Allowlist: printable ASCII except angle brackets and backticks. File names are repository
# content and must not be able to open or close a fence in the worker's prompt, nor smuggle
# invisible (zero-width, BOM, bidi, tag) characters or lone surrogates from a non-UTF-8 name
# (which would make the answer unencodable). Everything else becomes '?'.
_UNSAFE = re.compile(r"[^\x20-\x7e]|[<>`]")
_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

TOOL_DESCRIPTION = (
    "Optional file finder (Cairnwork, experimental): if you cannot tell which files hold the\n"
    f"code you need to read, end your reply with up to {MAX_REQUESTS} lines of the form\n"
    f"{REQUEST_PREFIX} <words, identifiers or paths you are looking for>\n"
    "and make no edits in that turn. You will get another turn with the best-matching repository\n"
    "paths (keyword ranking of the unmodified repository; it may miss files, so verify by reading\n"
    "them). Do not ask when the task already names the files."
)


@dataclass(frozen=True)
class FinderConfig:
    enabled: bool = False
    k: int = 5
    include_tests: bool = False

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> FinderConfig:
        env = os.environ if environ is None else environ
        return cls(enabled=env.get(ENV_FLAG, "") == "1")


def offer(instructions: str, config: FinderConfig) -> str:
    """``instructions`` with the tool description appended when the finder is enabled."""
    return f"{instructions}\n\n{TOOL_DESCRIPTION}" if config.enabled else instructions


def requests_in(reply: str) -> list[str]:
    """The worker's finder queries, in order, deduplicated, capped and length-limited."""
    out: list[str] = []
    for match in _REQUEST.finditer(reply):
        query = match.group(1).strip()[:MAX_QUERY_CHARS]
        if query and query not in out:
            out.append(query)
        if len(out) >= MAX_REQUESTS:
            break
    return out


def respond(repo: str | Path, base_commit: str, reply: str, config: FinderConfig) -> str | None:
    """The text to append for the worker's next turn, or None (finder off or no request).

    ``base_commit`` must be the task's full base commit id (never a branch or HEAD). The text
    is printable ASCII only, so it always encodes as UTF-8.
    """
    if not config.enabled:
        return None
    queries = requests_in(reply)
    if not queries:
        return None
    if not _SHA.match(base_commit):
        raise ValueError("base_commit must be a full 40- or 64-hex commit id")
    finder = FileFinder(commit_texts(repo, base_commit), config.include_tests)
    lines = ["Cairnwork file finder results (best match first; may be incomplete):"]
    for query in queries:
        hits = finder.find(query, config.k)
        lines.append(f"- for {_clean(query, MAX_QUERY_CHARS)!r}:")
        for i, hit in enumerate(hits, 1):
            lines.append(f"  {i}. {_clean(hit.path, MAX_PATH_CHARS)}")
        if not hits:
            lines.append("  (no matching file)")
    lines.append("Read the files you need, then make your edit in this turn.")
    return "\n".join(lines)


def _clean(text: str, limit: int) -> str:
    return _UNSAFE.sub("?", text)[:limit]
