"""Gateway-facing intake for one task type (council F): submit, status, cancel, drain.

The old Gateway keeps its entry and authentication and forwards only small bug
fixes here, through the ``cairnwork-route`` command (route.py). Everything lives
in one state directory (owner-only):

    ACCEPTING          present: new tasks are taken; absent (the default): refused
    config.json        {"model", "agy", "verifier_account", "verifier_python", "mirror",
                        "core_url", "core_token_file"}
    ownership.sqlite, notary.sqlite, journal.sqlite
    requests/<task_id>.json   the request as bound (its digest is checked again)
    runs/              attempt clones, verifier logs, one runner log per task

``submit`` binds the task (owner "cairnwork") while holding the shared run lock
and starts a runner process that inherits that lock, so restart recovery cannot
settle the task before the runner has it; if the runner dies, the kernel drops
the lock and ``recover`` settles the task (never re-runs it). A resend with the
same idempotency key returns the same task and starts nothing, also while
drained. Rollback = remove ACCEPTING: tasks in flight finish or are settled here;
nothing is re-run in another system.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cairnwork import runlock
from cairnwork.gitcmd import GitCommandError, run_git
from cairnwork.journal import Journal
from cairnwork.notary import Notary
from cairnwork.ownership import InvalidStatusTransition, OwnershipLedger, OwnershipUnknown
from cairnwork.pipeline import TaskSpec, check_task
from cairnwork.verifier_account import VerifierAccount
from cairnwork_protocol.http import CoreHttpError, check_core_url, http_core_verify

FIELDS = frozenset(
    {
        "idempotency_key",
        "card_hash",
        "title",
        "instructions",
        "repo",
        "base_commit",
        "allowed_files",
        "verifiers",
    }
)
_spawn = subprocess.Popen  # replaced in tests
# verifier_python: the target repository's own interpreter (scripts/setup_target_venv.sh),
# an absolute path; None runs verifiers with the account's (or Cairnwork's) Python.
# One per state directory, so one state directory serves one target repository.
DEFAULT_CONFIG = {
    "model": "gemini-3.8-flash-high",
    "agy": "agy",
    "verifier_account": True,
    "verifier_python": None,
    # Cairnwork's own repository for this target: the base is fetched into it from the
    # request's repo (read-only), and every ref or object Cairnwork writes lands here,
    # never in the caller's repository (e.g. the Gateway's live Controller).
    "mirror": None,
    # Nexus Core as an HTTP service (cairnwork_protocol.http) instead of in-process:
    # its root URL and a file holding its bearer token (never the token itself).
    "core_url": None,
    "core_token_file": None,
}


class IntakeRefused(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def task_from_payload(payload: Any) -> TaskSpec:
    """A closed schema: unknown or missing fields are refused, never ignored."""
    if not isinstance(payload, dict) or not FIELDS <= set(payload) <= FIELDS | {"test_commit"}:
        got = sorted(payload) if isinstance(payload, dict) else type(payload).__name__
        raise IntakeRefused(
            "BAD_REQUEST", f"fields must be {sorted(FIELDS)} (+test_commit), got {got}"
        )
    if not isinstance(payload.get("test_commit", ""), str):
        raise IntakeRefused("BAD_REQUEST", "test_commit must be a string")
    texts = ("idempotency_key", "card_hash", "title", "instructions", "repo", "base_commit")
    if not all(isinstance(payload[name], str) for name in texts):
        raise IntakeRefused("BAD_REQUEST", "text fields must be strings")
    files, verifiers = payload["allowed_files"], payload["verifiers"]
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        raise IntakeRefused("BAD_REQUEST", "allowed_files must be a list of strings")
    if not isinstance(verifiers, list) or not all(_verifier_shape(v) for v in verifiers):
        raise IntakeRefused("BAD_REQUEST", "verifiers must be [[id, [argv...]], ...]")
    if not os.path.isabs(payload["repo"]):
        raise IntakeRefused("BAD_REQUEST", "repo must be an absolute path")
    return TaskSpec(
        idempotency_key=payload["idempotency_key"],
        card_hash=payload["card_hash"],
        title=payload["title"],
        instructions=payload["instructions"],
        repo=Path(payload["repo"]),
        base_commit=payload["base_commit"],
        allowed_files=tuple(files),
        verifiers=tuple((v[0], tuple(v[1])) for v in verifiers),
        test_commit=payload.get("test_commit", ""),
    )


def _verifier_shape(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], list)
        and all(isinstance(a, str) for a in value[1])
    )


def _has_token(token_file: Any) -> bool:
    """A regular file holding a token; it is read again at each Core call (rotation)."""
    try:
        path = Path(token_file)
        return path.is_file() and bool(path.read_text(encoding="utf-8").strip())
    except (OSError, TypeError, ValueError):
        return False


@dataclass(frozen=True)
class Intake:
    state: Path

    @property
    def ledger(self) -> OwnershipLedger:
        return OwnershipLedger(self.state / "ownership.sqlite")

    @property
    def notary(self) -> Notary:
        config = self.config()  # a Core HTTP service when configured, else in-process
        if config["core_url"] is None:
            return Notary(self.state / "notary.sqlite")
        verify = http_core_verify(config["core_url"], Path(config["core_token_file"]))
        return Notary(self.state / "notary.sqlite", core_verify=verify)

    @property
    def journal(self) -> Journal:
        return Journal(self.state / "journal.sqlite")

    @property
    def run_root(self) -> Path:
        return self.state / "runs"

    def config(self) -> dict[str, Any]:
        path = self.state / "config.json"
        loaded = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(loaded, dict) or set(loaded) - set(DEFAULT_CONFIG):
            raise IntakeRefused("BAD_CONFIG", f"{path} keys must be among {sorted(DEFAULT_CONFIG)}")
        for key in ("verifier_python", "mirror", "core_token_file"):
            value = loaded.get(key)
            if value is not None and not (isinstance(value, str) and os.path.isabs(value)):
                raise IntakeRefused("BAD_CONFIG", f"{key} must be an absolute path")
        if loaded.get("core_url") is not None:
            try:
                check_core_url(loaded["core_url"])
            except CoreHttpError as exc:
                raise IntakeRefused("BAD_CONFIG", str(exc)) from exc
            if not _has_token(loaded.get("core_token_file")):
                raise IntakeRefused("BAD_CONFIG", "core_url needs a readable, non-empty token file")
        return {**DEFAULT_CONFIG, **loaded}

    def verifier_account(self) -> VerifierAccount | None:
        return VerifierAccount() if self.config()["verifier_account"] else None

    def accepting(self) -> bool:
        return (self.state / "ACCEPTING").exists()

    def set_accepting(self, on: bool) -> None:
        """Under the intake lock: once drain returns, no submit that checked the flag
        before it can still bind (each submit holds the lock from check to bind)."""
        with self._intake_lock():
            if on:
                (self.state / "ACCEPTING").touch(mode=0o600)
            else:
                (self.state / "ACCEPTING").unlink(missing_ok=True)

    def prepare(self) -> None:
        """Create the state directory owner-only, or refuse one anyone else can use."""
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.state.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise IntakeRefused("BAD_STATE", f"{self.state} is not a directory owned by us")
        if info.st_mode & 0o077:
            raise IntakeRefused("BAD_STATE", f"{self.state} must be mode 0700")
        for name in ("requests", "runs"):
            (self.state / name).mkdir(mode=0o700, exist_ok=True)

    @contextmanager
    def _intake_lock(self) -> Iterator[None]:
        self.prepare()
        fd = os.open(self.state / "intake.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def submit(self, payload: Any) -> dict[str, Any]:
        task = task_from_payload(payload)
        check_task(task)
        mirror = self.config()["mirror"]  # a bad config is refused here, not by the runner
        source = task.repo
        if mirror:  # the task is bound to Cairnwork's own repository, whatever the caller's
            if not Path(mirror).is_dir():
                raise IntakeRefused("BAD_CONFIG", f"mirror {mirror} does not exist")
            payload = {**payload, "repo": mirror}
            task = task_from_payload(payload)
        with self._intake_lock():
            return self._bind_and_start(task, payload, source)

    def _bind_and_start(self, task: TaskSpec, payload: dict[str, Any], source: Path):
        ledger = self.ledger
        try:
            known = ledger.get(idempotency_key=task.idempotency_key)
        except OwnershipUnknown:
            known = None
        if known is None and not self.accepting():
            raise IntakeRefused("NOT_ACCEPTING", "Cairnwork takes no new tasks")
        if known is None and source.resolve() != task.repo.resolve():
            _mirror_base(task.repo, source, task.base_commit)  # new work only, under the lock
        fd = runlock.shared_fd(ledger.path)
        try:
            bound = ledger.bind(task.idempotency_key, task.request(), "cairnwork", task.card_hash)
            record = bound.record
            if not bound.duplicate:
                try:
                    self._store_request(record.task_id, payload)
                    self._start_runner(record.task_id, fd)
                except Exception:
                    ledger.set_status(record.task_id, "FAILED")  # never left BOUND, unowned
                    raise
        finally:
            os.close(fd)  # the runner holds its own copy of the lock now
        return {"task_id": record.task_id, "status": record.status, "duplicate": bound.duplicate}

    def _store_request(self, task_id: str, payload: dict[str, Any]) -> None:
        path = self.state / "requests" / f"{task_id}.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, sort_keys=True)

    def load_request(self, task_id: str) -> TaskSpec:
        path = self.state / "requests" / f"{task_id}.json"
        return task_from_payload(json.loads(path.read_text()))

    def _start_runner(self, task_id: str, lock_fd: int) -> None:
        log = self.run_root / f"{task_id}.runner.log"
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            _spawn(  # noqa: S603 - fixed argv, no shell
                [
                    sys.executable,
                    "-I",  # no cwd on sys.path, no PYTHON* env: the caller's tree
                    "-m",  # (e.g. the Gateway's, which has its own `product`) cannot shadow
                    "cairnwork.route",
                    "--state",
                    str(self.state),
                    "_run",
                    "--task-id",
                    task_id,
                    "--lock-fd",
                    str(lock_fd),
                ],
                pass_fds=(lock_fd,),
                cwd=self.state,
                stdin=subprocess.DEVNULL,
                stdout=fd,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            os.close(fd)

    def status(self, *, task_id: str | None = None, key: str | None = None) -> dict[str, Any]:
        record = self.ledger.get(task_id=task_id, idempotency_key=key)
        entries = self.journal.entries(record.task_id)
        attempt = entries[-1].attempt_id if entries else ""
        phases = [e for e in entries if e.attempt_id == attempt]
        candidates = [e.detail for e in phases if e.phase == "CANDIDATE"]
        verified = False
        if candidates:
            verdict = self.notary.verdict(
                record.task_id, attempt, expected_candidate=candidates[-1]
            )
            verified = bool(verdict and verdict.verified)
        return {
            "task_id": record.task_id,
            "idempotency_key": record.idempotency_key,
            "owner": record.owner,
            "status": record.status,
            "attempt_id": attempt,
            "phase": phases[-1].phase if phases else "",
            "detail": phases[-1].detail if phases else "",
            "candidate_commit": candidates[-1] if candidates else "",
            "core_verified": verified,
        }

    def cancel(self, task_id: str, owner: str) -> dict[str, Any]:
        """Only a task that has not started can be cancelled; a running one finishes.
        Compare-and-set: a runner that dispatched first wins, cancel is refused.
        The caller states its owner; a task bound to another owner is refused."""
        ledger = self.ledger
        if ledger.get(task_id=task_id).owner != owner:  # UnknownTask comes first
            raise IntakeRefused("OWNER_MISMATCH", f"{task_id} is not owned by {owner!r}")
        try:
            record = ledger.set_status(task_id, "CANCELLED", expected_from="BOUND")
        except InvalidStatusTransition as exc:
            raise IntakeRefused("NOT_CANCELLABLE", str(exc)) from exc
        return {"task_id": task_id, "status": record.status}

    def in_flight(self) -> dict[str, Any]:
        """A missing state directory is an error, not "nothing in flight" (fail closed)."""
        if not self.state.is_dir():
            raise IntakeRefused("BAD_STATE", f"{self.state} does not exist")
        records = self.ledger.in_flight() if (self.state / "ownership.sqlite").exists() else []
        return {
            "accepting": self.accepting(),
            "count": len(records),
            "task_ids": [r.task_id for r in records],
        }


def _mirror_base(mirror: Path, source: Path, base: str) -> None:
    """Fetch base from source into the mirror and pin it with a ref (no FETCH_HEAD, so
    no lock between parallel fetches, and gc cannot drop it). Only source is read."""
    try:
        run_git(
            mirror, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head", str(source),
            f"{base}:refs/cairnwork-bases/{base}",  # outside the guarded refs/cairnwork/
        )  # fmt: skip
    except GitCommandError as exc:
        raise IntakeRefused("BAD_REQUEST", f"cannot fetch {base} from {source}") from exc
