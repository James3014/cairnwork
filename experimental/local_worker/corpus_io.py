"""The only file I/O of the local worker loop (#88, Plan v2 points 1, 6, 9, 10).

The loop opens task.json and the plan file (its inputs), the canary report (which must sit inside
the output directory) and the output directory itself; it hashes the installed wrapper and the
repository copy of it to bind the canary, and asks git for the harness commit. Nothing here reads
the checkout: what the model sees comes only from worker tool output. Every path this module opens
is first checked with ``_inside`` against its root, except the fixed wrapper paths and task.json.

Output layout (``<out>``): ``manifest.json`` (frozen settings), ``episodes-{dataset}-{model}-{split}
.jsonl`` (append-only, one validated line per episode), and ``private/<episode_id>/`` (mode 0700:
transcript.json with the messages and raw usage, calls.jsonl from the tool layer).
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from experimental.local_worker.agent import Task
from experimental.local_worker.canary_check import CanaryEvidenceError, check_evidence
from experimental.local_worker.episode_schema import validate_record
from experimental.local_worker.tools import INSTALLED_WRAPPER

REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_WRAPPER = Path(__file__).with_name("local-worker-tool")
CANARY_MAX_AGE = timedelta(days=7)
# v1 includes wall_time_rule (PR #248); no manifest was recorded before it (see SCHEMA_VERSION).
MANIFEST_SCHEMA = "cairnwork.local_worker.manifest/v1"
UNFROZEN = ("canary", "created_at")  # may change between episodes of one manifest
ID_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class InputRefused(ValueError):
    """An input, binding or manifest check failed: the loop does not start."""


def _inside(path: Path, root: Path) -> Path:
    real, base = path.resolve(), root.resolve()
    if real != base and base not in real.parents:
        raise InputRefused(f"{path} is not inside {root}")
    return real


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _target_ok(target: Any) -> bool:
    if not isinstance(target, str) or not target or target.startswith(("-", "/")):
        return False
    path = target.split("::", 1)[0]
    return ".." not in path.split("/") and not re.search(r"[\s\0]", target)


def load_task(path: Path) -> Task:
    """task.json: {task_id, repo (anonymised codes), prompt, public_tests: [pytest targets]}."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InputRefused(f"task file unreadable: {exc}") from None
    if not isinstance(data, dict):
        raise InputRefused("task file is not a JSON object")
    for key in ("task_id", "repo"):
        if not isinstance(data.get(key), str) or not ID_RE.fullmatch(data[key]):
            raise InputRefused(f"task {key} must match {ID_RE.pattern} (anonymised by the caller)")
    if not isinstance(data.get("prompt"), str) or not data["prompt"].strip():
        raise InputRefused("task prompt is missing")
    targets = data.get("public_tests")
    if not isinstance(targets, list) or not targets or not all(map(_target_ok, targets)):
        raise InputRefused("public_tests must be a non-empty list of relative pytest targets")
    return Task(data["task_id"], data["repo"], data["prompt"], tuple(targets))


def load_plan(path: Path, out_dir: Path) -> str:
    return _inside(path, out_dir).read_text(encoding="utf-8")


def load_transcript(path: Path, out_dir: Path) -> dict:
    data = json.loads(_inside(path, out_dir / "private").read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise InputRefused("parent transcript is not a JSON object")
    return data


def harness_version() -> str:
    """The commit of this harness, with ``-dirty`` when tracked files differ from it."""

    def git(*args: str) -> str:
        out = subprocess.run(  # noqa: S603 - fixed argv on the harness repo, never the checkout
            ["/usr/bin/git", "-C", str(REPO_ROOT), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        return out.stdout.strip()

    head = git("rev-parse", "HEAD") or "unknown"
    return head + ("-dirty" if git("status", "--porcelain", "--untracked-files=no") else "")


def _wrapper_sha(path: Path) -> str | None:
    return sha256_bytes(path.read_bytes()) if path.is_file() else None


def _finished_at(value: Any) -> datetime:
    """The report's finish time: an ISO timestamp with a UTC offset, else refused (a naive one
    cannot be compared with an aware now; Claude review of #239, NB10)."""
    try:
        when = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        when = None
    if when is None or when.utcoffset() is None:
        raise InputRefused(f"canary report finished_at {value!r} is not a time with a UTC offset")
    return when


def verify_canary(
    report_path: Path,
    out_dir: Path,
    *,
    wrapper: Path = INSTALLED_WRAPPER,
    host: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Plan v2 point 9: the report's recomputed sha256 matches its sidecar; its content is a
    complete passing worker-mode canary (canary_check.check_evidence: schema, all six checks
    and every required probe passed, the worker identity); bound to the installed wrapper
    (whose sha256 also equals this repository's copy, so the installed tool configuration is
    the one this loop expects), to this host, and at most 7 days old. Returns the binding
    recorded in the manifest and in every line.

    The sidecar sha256 proves integrity only (the report was not changed after it was written);
    it is not a signature and proves nothing about who wrote the report. A report dated in the
    future (beyond 5 minutes of clock skew) is refused like a stale one."""
    data = _inside(report_path, out_dir).read_bytes()
    digest = sha256_bytes(data)
    sidecar = report_path.with_name(report_path.name + ".sha256")
    recorded = _inside(sidecar, out_dir).read_text().split()[:1]
    if recorded != [digest]:
        raise InputRefused("canary report sha256 does not match its .sha256 sidecar")
    report = json.loads(data)
    try:  # Codex B5: the checks themselves, not only overall and the metadata
        check_evidence(report)
    except CanaryEvidenceError as exc:
        raise InputRefused(f"canary report: {exc}") from None
    installed = _wrapper_sha(wrapper)
    expected = {
        "overall": "PASS",
        "mode": "worker",
        "wrapper": str(wrapper),
        "wrapper_sha256": installed,
        "host": host or platform.node(),
    }
    for key, want in expected.items():
        if want is None or report.get(key) != want:
            raise InputRefused(f"canary report {key}={report.get(key)!r}, expected {want!r}")
    if installed != _wrapper_sha(REPO_WRAPPER):
        raise InputRefused("installed wrapper differs from this repository's local-worker-tool")
    finished = _finished_at(report.get("finished_at"))
    age = (now or datetime.now(UTC)) - finished
    if not timedelta(minutes=-5) <= age <= CANARY_MAX_AGE:
        raise InputRefused(f"canary report is {age} old (limit {CANARY_MAX_AGE})")
    return {
        "report": report_path.name,
        "report_sha256": digest,
        "finished_at": report["finished_at"],
        "wrapper_sha256": installed,
        "host": expected["host"],
    }


def prepare_manifest(out_dir: Path, manifest: dict) -> str:
    """Write manifest.json once; later episodes must match every frozen field. Returns its sha."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = _inside(out_dir / "manifest.json", out_dir)
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        keys = (set(old) | set(manifest)) - set(UNFROZEN)
        diff = sorted(k for k in keys if old.get(k) != manifest.get(k))
        if diff:
            raise InputRefused(f"manifest.json is frozen; these fields differ: {diff}")
    else:
        data = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        with path.open("x", encoding="utf-8") as fh:
            fh.write(data)
    return sha256_bytes(path.read_bytes())


def private_dir(out_dir: Path, episode_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", episode_id):
        raise InputRefused(f"bad episode id {episode_id!r}")
    root = _inside(out_dir / "private", out_dir)
    root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    path = _inside(root / episode_id, root)
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return path


def write_private_json(directory: Path, name: str, obj: Any) -> Path:
    path = _inside(directory / name, directory)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, sort_keys=True, default=str)
    return path


def episodes_path(out_dir: Path, dataset: str, model: str, split: str) -> Path:
    slug = "-".join(re.sub(r"[^A-Za-z0-9_.]+", "_", s) for s in (dataset, model, split))
    return _inside(out_dir / f"episodes-{slug}.jsonl", out_dir)


def used_checkouts(out_dir: Path) -> set[str]:
    """The checkout_id (run root name) of every line already in a corpus file under out_dir."""
    ids: set[str] = set()
    for path in sorted(out_dir.glob("episodes-*.jsonl")):
        for line in _inside(path, out_dir).read_text(encoding="utf-8").splitlines():
            ids.add(json.loads(line).get("checkout_id"))
    return ids


def append_episode(path: Path, record: dict, out_dir: Path) -> str:
    """Validate, then append one line (O_APPEND, fsync). Returns the line's sha256."""
    validate_record(record)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True)
    if "\n" in line or json.loads(line) != record:
        raise InputRefused("episode record does not round-trip as one JSON line")
    data = (line + "\n").encode("utf-8")
    fd = os.open(_inside(path, out_dir), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)
    return sha256_bytes(data)
