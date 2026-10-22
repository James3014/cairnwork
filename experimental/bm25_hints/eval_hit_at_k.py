"""Offline hit@k of ``rank_files`` over a task bank (aggregate numbers only).

Bank layout: ``<bank>/tasks/<id>/{meta.json,issue.md,deferred_fixcheck.json,issue_check.json}``
and a source clone at ``<bank>/src/<repo>``. A task is usable when issue.md exists,
deferred_fixcheck.json says ``ok`` and issue_check.json has no ``exclude``/``drop``.
Each base commit is checked out in a throwaway ``--shared`` clone (the source clone
is never written). Per-task detail goes to stderr only with ``--verbose`` and never
reaches the JSON on stdout.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from .retrieval import rank_files

KS = (1, 5, 10)


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def usable_tasks(bank: Path) -> list[Path]:
    out = []
    for d in sorted((bank / "tasks").iterdir()):
        check = _load(d / "issue_check.json")
        if (
            (d / "issue.md").is_file()
            and _load(d / "deferred_fixcheck.json").get("ok") is True
            and not check.get("exclude")
            and not check.get("drop")
            and (d / "meta.json").is_file()
        ):
            out.append(d)
    return out


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


def ranked_at_base(bank: Path, task: Path, meta: dict, k: int) -> list[str]:
    clone = bank / "src" / meta["repo"]
    with tempfile.TemporaryDirectory(prefix="bm25-eval-") as tmp:
        work = str(Path(tmp) / "co")
        _git("clone", "-q", "--no-checkout", "--shared", str(clone), work)
        _git("-C", work, "checkout", "-q", "--detach", meta["base_sha"])
        text = (task / "issue.md").read_text(encoding="utf-8", errors="replace")
        return rank_files(work, text, k=k)


def evaluate(bank: Path, verbose: bool = False) -> dict:
    hits = {k: 0 for k in KS}
    full = {k: 0 for k in KS}
    done = skipped = 0
    for task in usable_tasks(bank):
        meta = _load(task / "meta.json")
        want = set(meta.get("files_changed") or [])
        try:
            ranked = ranked_at_base(bank, task, meta, max(KS))
        except (subprocess.CalledProcessError, KeyError, OSError):
            skipped += 1
            continue
        if not want:
            skipped += 1
            continue
        done += 1
        for k in KS:
            top = set(ranked[:k])
            hits[k] += bool(want & top)
            full[k] += want <= top
        if verbose:
            print(f"{task.name}: first_hit_rank={_first(ranked, want)}", file=sys.stderr)
    rate = lambda n: round(n / done, 4) if done else 0.0  # noqa: E731
    return {
        "tasks": done,
        "skipped": skipped,
        **{f"hit@{k}": hits[k] for k in KS},
        **{f"hit@{k}_rate": rate(hits[k]) for k in KS},
        **{f"all_files@{k}": full[k] for k in KS},
    }


def _first(ranked: list[str], want: set[str]) -> int | None:
    return next((i + 1 for i, p in enumerate(ranked) if p in want), None)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m experimental.bm25_hints.eval_hit_at_k")
    ap.add_argument("bank", type=Path)
    ap.add_argument("--verbose", action="store_true", help="per-task ranks to stderr")
    args = ap.parse_args(argv)
    print(json.dumps(evaluate(args.bank, args.verbose), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
