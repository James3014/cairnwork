"""Gateway-facing intake for one task type (council F): submit, status, cancel, drain.

The old Gateway keeps its entry and authentication and forwards only small bug
fixes here, through the ``cairnwork-route`` command (route.py). Everything lives
in one state directory (owner-only):

    ACCEPTING          present: new tasks are taken; absent (the default): refused
    config.json        {"model", "agy", "agy_dispatch", "agy_dispatch_*_seconds",
                        "agy_run_as_user", "verifier_account", "verifier_python", "gh",
                        "mirror", "core_url", "core_token_file", "core_identity", "test_roots",
                        "import_paths", "post_verified_hook", "post_verified_hook_seconds"}
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
import shutil
import stat
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from cairnwork import runlock
from cairnwork.deferred_store import DeferredStore
from cairnwork.gitcmd import GitCommandError, run_git
from cairnwork.intake_config import DEFAULT_CONFIG, has_token, seconds_ok
from cairnwork.intake_request import IntakeRefused, task_from_payload
from cairnwork.issue_task import check_import_paths, check_test_roots
from cairnwork.journal import Journal
from cairnwork.lineage import require_on_main
from cairnwork.notary import Notary
from cairnwork.ownership import (
    OWNER,
    InvalidStatusTransition,
    OwnershipLedger,
    OwnershipUnknown,
)
from cairnwork.p2p_selection import check_p2p_config
from cairnwork.pipeline import TaskSpec, check_task
from cairnwork.taskpaths import check_submitted_paths
from cairnwork.verifier_account import VerifierAccount
from cairnwork.workers.agy_dispatch import valid_run_as_user
from cairnwork_protocol.http import CoreHttpError, check_core_url, http_core_verify

_spawn = subprocess.Popen  # replaced in tests


@dataclass(frozen=True)
class Intake:
    state: Path

    @property
    def ledger(self) -> OwnershipLedger:
        return OwnershipLedger(self.state / "ownership.sqlite")

    @property
    def notary(self) -> Notary:
        config = self.config()  # a Core HTTP service when configured, else in-process
        verify = None
        if config["core_url"] is not None:
            verify = http_core_verify(config["core_url"], Path(config["core_token_file"]))
        return Notary(
            self.state / "notary.sqlite", core_verify=verify, core_identity=config["core_identity"]
        )

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
        for key in (
            "verifier_python",
            "mirror",
            "core_token_file",
            "agy_dispatch",
            "gh",
            "post_verified_hook",
        ):
            value = loaded.get(key)
            if value is not None and not (isinstance(value, str) and os.path.isabs(value)):
                raise IntakeRefused("BAD_CONFIG", f"{key} must be an absolute path")
        model = loaded.get("model", DEFAULT_CONFIG["model"])
        if not isinstance(model, str) or not model or any(c in model for c in "\n\r\0"):
            raise IntakeRefused("BAD_CONFIG", "model must be a one-line string")
        for key in DEFAULT_CONFIG.keys() & loaded.keys():
            if (
                key.startswith("agy_dispatch_") or key == "post_verified_hook_seconds"
            ) and not seconds_ok(loaded[key]):
                raise IntakeRefused("BAD_CONFIG", f"{key} must be a whole number of seconds")
        if not valid_run_as_user(loaded):  # an account name, and only with agy_dispatch
            raise IntakeRefused("BAD_CONFIG", "agy_run_as_user: an account name, with agy_dispatch")
        if loaded.get("core_identity") is not None and not isinstance(
            loaded["core_identity"], dict
        ):
            raise IntakeRefused("BAD_CONFIG", "core_identity must be an object")
        if loaded.get("core_url") is not None:
            try:
                check_core_url(loaded["core_url"])
            except CoreHttpError as exc:
                raise IntakeRefused("BAD_CONFIG", str(exc)) from exc
            if not has_token(loaded.get("core_token_file")):
                raise IntakeRefused("BAD_CONFIG", "core_url needs a readable, non-empty token file")
        if not isinstance(loaded.get("require_base_on_main", True), bool):
            raise IntakeRefused("BAD_CONFIG", "require_base_on_main must be true or false")
        if loaded.get("test_source", "auto") not in ("auto", "bundle"):
            raise IntakeRefused("BAD_CONFIG", 'test_source must be "auto" or "bundle"')
        try:
            check_test_roots(loaded.get("test_roots", ["tests"]))
            check_import_paths(loaded.get("import_paths", []))
            check_p2p_config(loaded)
        except ValueError as exc:
            raise IntakeRefused("BAD_CONFIG", str(exc)) from exc
        return {**DEFAULT_CONFIG, **loaded}

    def remember_gh(self, gh: str) -> None:
        """Record the (absolute) gh the Issue runner uses, for the runner that submit starts.
        Call it under issues.lock: the config.json write is not otherwise serialised."""
        resolved = shutil.which(gh)
        if resolved is None or not os.path.isabs(resolved):
            return  # a gh we cannot name absolutely is not recorded (it would be BAD_CONFIG)
        path = self.state / "config.json"
        loaded = json.loads(path.read_text()) if path.exists() else {}
        if isinstance(loaded, dict) and loaded.get("gh") != resolved:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({**loaded, "gh": resolved}, sort_keys=True))
            tmp.chmod(0o600)
            tmp.replace(path)

    def clone_problem(self, clone: Path) -> str:
        """#234: a clone the Issue runner works in must be the state's mirror, compared by real
        path (a symlink alias is the same clone; another clone of the same origin is not, and its
        refs and config can differ). "" when it is, or when the state names no mirror."""
        mirror = self.config()["mirror"]
        if mirror and os.path.realpath(mirror) != os.path.realpath(clone):
            return f"CLONE_NOT_STATE_MIRROR: {clone} is not the state's mirror {mirror}"
        return ""

    def test_store(self) -> DeferredStore:
        return DeferredStore.for_state(self.state, self.config().get("test_source"))

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
        for name in ("requests", "runs", "deferred-tests"):
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
        config = self.config()
        task = task_from_payload(
            payload,
            tuple(config["test_roots"]),
            self.test_store(),
            tuple(config["import_paths"]),
        )
        python = config["verifier_python"] or getattr(self.verifier_account(), "python", None)
        check_submitted_paths(task, check_task(task, verifier_python=python))
        mirror = config["mirror"]  # a bad config is refused here, not by the runner
        source = task.repo
        if mirror:  # the task is bound to Cairnwork's own repository, whatever the caller's
            if not Path(mirror).is_dir():
                raise IntakeRefused("BAD_CONFIG", f"mirror {mirror} does not exist")
            payload = {**payload, "repo": mirror}
            task = task_from_payload(payload, task.test_roots, self.test_store(), task.import_paths)
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
        if known is None and self.config()["require_base_on_main"]:
            try:  # fetches origin/main of the repository the task is bound to
                require_on_main(task.repo, task.base_commit)
            except RuntimeError as exc:
                if str(exc).startswith("LINEAGE_FETCH_FAILED"):
                    raise IntakeRefused(
                        "ORIGIN_FETCH_FAILED",
                        f"{exc}; set require_base_on_main false in config.json "
                        "if the repository has no origin",
                    ) from exc
                raise IntakeRefused("BASE_NOT_ON_MAIN", str(exc)) from exc
        fd = runlock.shared_fd(ledger.path)
        try:
            bound = ledger.bind(task.idempotency_key, task.request(), OWNER, task.card_hash)
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
        raw, config = json.loads(path.read_text()), self.config()
        task = task_from_payload(
            raw, tuple(config["test_roots"]), self.test_store(), tuple(config["import_paths"])
        )
        p2p = {"p2p_mode": config["p2p_selection"], "p2p_budget": config["p2p_time_budget_seconds"]}
        return replace(task, **p2p)

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

    def cancel(self, task_id: str) -> dict[str, Any]:
        """Only a task that has not started can be cancelled; a running one finishes.
        Compare-and-set: a runner that dispatched first wins, cancel is refused."""
        ledger = self.ledger
        if ledger.get(task_id=task_id).owner != OWNER:  # UnknownTask first; not ours: refused
            raise IntakeRefused("OWNER_MISMATCH", f"{task_id} is not owned by {OWNER!r}")
        try:
            record = ledger.set_status(task_id, "CANCELLED", expected_from="BOUND")
        except InvalidStatusTransition as exc:
            raise IntakeRefused("NOT_CANCELLABLE", str(exc)) from exc
        return {"task_id": task_id, "status": record.status}

    def in_flight(self) -> dict[str, Any]:
        """A missing state directory is an error, not "nothing in flight" (fail closed)."""
        if not self.state.is_dir():
            raise IntakeRefused("BAD_STATE", f"{self.state} does not exist")
        has_ledger = (self.state / "ownership.sqlite").exists()
        records = self.ledger.in_flight() if has_ledger else []
        foreign = self.ledger.foreign_in_flight() if has_ledger else []
        return {
            "accepting": self.accepting(),
            "count": len(records),
            "task_ids": [r.task_id for r in records],
            "foreign_task_ids": [r.task_id for r in foreign],  # reported, never counted
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
