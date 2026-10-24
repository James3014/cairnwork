"""Verify a candidate commit made elsewhere (another agent, CI) with the checks Cairnwork
applies to its own worker's output: one commit on the base that touches only the allowed
files (no deletion, symlink or submodule), the deferred tests laid over it, the verifiers
in the sandbox, and a Core verdict bound to the exact commit.

No worker runs and nothing is bound in the ownership ledger, so the deployer's in-flight
count never sees it. The run holds the shared run lock, as any run does, so ``recover``
cannot mistake its verifier processes for a dead run's. Like ``submit`` it is refused
while the state is drained (checked again once the run lock is held). Every check runs in
a scratch clone of the state's ``mirror`` (or of --repo-path when there is none) that this
run alone owns; the external candidate is fetched into the scratch clone only, never into
the shared mirror or the caller's repository. The Core record
goes to the state's notary under a one-off ``verify-`` task id.

A run marks itself with a locked file in ``<state>/verify-runs/``; the kernel drops the
lock when the run dies, so ``cleanup`` (also done at the start of every run) can tell a
dead run's leftovers (clones, baseline dirs, orphan processes) from a live run's.
"""

from __future__ import annotations

import fcntl
import os
import secrets
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from cairnwork import runlock
from cairnwork.account_check import base_stage, precheck
from cairnwork.confined import ATTEMPT_ENV, sweep_marked
from cairnwork.deferred_tests import (
    BASE_COMPARE_RULE,
    OVERLAY_RULE,
    BaseUncomparable,
    TestCommitRejected,
    check_roots_on_base,
    lay_tests_over,
    never_seen,
)
from cairnwork.gitcmd import OID, GitCommandError, run_git
from cairnwork.intake import Intake, IntakeRefused
from cairnwork.notary_verify import binding_hash
from cairnwork.ownership import request_digest
from cairnwork.p2p import p2p_for
from cairnwork.pipeline import (
    TaskRejected,
    TaskResult,
    TaskSpec,
    check_task,
    judge_candidate,
    tests_with_teeth,
)
from cairnwork.recovery import _remove_leftovers
from cairnwork.workspace import (
    ATTEMPT_ID,
    Attempt,
    CandidateRejected,
    WorkspaceError,
    check_candidate_commit,
    create_attempt_clone,
    remove_attempt_clone,
)


def verify_candidate(
    intake: Intake,
    *,
    repo: Path,
    base: str,
    candidate: str,
    allowed_files: tuple[str, ...],
    verifiers: tuple[tuple[str, tuple[str, ...]], ...],
    test_commit: str = "",
) -> dict[str, Any]:
    """``verified`` is true only for a Core verdict bound to what was judged: the candidate, or
    with deferred tests the tree that ran (candidate plus tests) and its ``binding`` (#229), whose
    head is the candidate. ``status`` is VERIFIED, FAILED, REFUSED or UNCOMPARABLE (#234: the base
    could not be compared; ``uncomparable`` says why, Core is never asked). The account precheck
    runs before the test commit is burned; ``tests_consumed`` says whether the test commit is now
    used up (a retry needs a new one). Raises TaskRejected for malformed input and IntakeRefused
    when drained or the mirror cannot be used."""
    task = TaskSpec(
        f"verify-{secrets.token_hex(8)}", "verify", "verify", "", repo, base,
        allowed_files, verifiers, test_commit=test_commit,
        test_roots=tuple(intake.config()["test_roots"]), test_store=intake.test_store(),
        p2p_mode=intake.config()["p2p_selection"],
        p2p_budget=intake.config()["p2p_time_budget_seconds"],
    )  # fmt: skip
    account = intake.verifier_account()
    python = intake.config()["verifier_python"]
    specs = check_task(
        task, verifier_python=python or getattr(account, "python", None)
    )  # the same scope, protected-file and verifier rules
    if not OID.fullmatch(candidate):
        raise TaskRejected("BAD_CANDIDATE", "candidate must be a 40-hex commit id")
    _require_accepting(intake)
    source = _source(intake, repo)
    intake.prepare()
    cleanup(intake)
    result = TaskResult(task.idempotency_key, f"a{secrets.token_hex(8)}", "FAILED")
    notary = intake.notary
    verifying = (account, python)
    status, binding, uncomparable = "FAILED", None, None
    with runlock.held(intake.state / "ownership.sqlite", exclusive=False):
        _require_accepting(intake)  # a drain that came in while we waited for the lock
        with _marked(intake, result.attempt_id):
            try:
                precheck(*verifying)  # before _burn: an unusable account consumes no tests
                binding = _judge(intake, task, source, specs, candidate, notary, result, verifying)
            except (CandidateRejected, TestCommitRejected, WorkspaceError) as exc:
                result.reason, status = str(exc), "REFUSED"
            except BaseUncomparable as exc:  # before Core: nothing notarised, nothing to sign
                result.reason, status = f"UNCOMPARABLE: {exc}", "UNCOMPARABLE"
                found, rule = exc.comparison, BASE_COMPARE_RULE
                uncomparable = {"code": found.code, "detail": found.detail, "rule": rule}
    judged = result.candidate_commit or candidate
    subject = binding["head"] if binding else judged  # a bound row names the head (#229)
    verdict = notary.verdict(result.task_id, result.attempt_id, expected_candidate=subject)
    return {
        "status": "VERIFIED" if result.verified else status,
        "verified": result.verified,
        "reason": result.reason,
        "core_status": verdict.status if verdict else "",
        "candidate_commit": candidate,
        "judged_commit": judged,  # with tests: a commit of the scratch clone only, gone now
        "executed_tree": binding["executed_tree"] if binding else "",
        "binding": binding,
        "task_id": result.task_id,
        "attempt_id": result.attempt_id,
        "logs": result.logs,
        "p2p": result.p2p,
        "uncomparable": uncomparable,  # {code, detail, rule} when status is UNCOMPARABLE (#234)
        "tests_consumed": tests_consumed(intake, test_commit),
    }


def _require_accepting(intake: Intake) -> None:
    if not intake.accepting():
        raise IntakeRefused("VERIFY_NOT_ACCEPTING", "Cairnwork takes no new work (drained)")


def _source(intake: Intake, repo: Path) -> Path:
    """The repository the worker path would use as task.repo: the state's mirror when it
    names one, else the caller's. It is only read."""
    mirror = intake.config()["mirror"]
    if mirror and not Path(mirror).is_dir():
        raise IntakeRefused("BAD_CONFIG", f"mirror {mirror} does not exist")
    return Path(mirror) if mirror else repo


@contextmanager
def _scratch(intake: Intake, source: Path, repo: Path, task: TaskSpec, candidate: str, name: str):
    """A clone of source for this run alone. The candidate comes from outside, so it is
    fetched into this clone only, never into the shared mirror (objects there would later
    make a test commit look already seen). Its origin is source's origin, so a deferred
    test commit is fetched from the same place as on the worker path."""
    path = intake.state / "verify-scratch" / name
    path.parent.mkdir(mode=0o700, exist_ok=True)
    try:
        run_git(path.parent, "clone", "--quiet", "--no-local", "--no-checkout", str(source), name)
        try:
            run_git(
                path,
                "remote",
                "set-url",
                "origin",
                _absolute(run_git(source, "remote", "get-url", "origin").strip(), source),
            )
        except GitCommandError:
            run_git(path, "remote", "remove", "origin")  # source has none: neither do we
        run_git(path, "fetch", "--quiet", "--no-tags", str(repo), task.base_commit, candidate)
    except GitCommandError as exc:
        shutil.rmtree(path, ignore_errors=True)
        raise WorkspaceError(f"cannot prepare a scratch clone: {exc}") from exc
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _check_parent(repo: Path, base: str, candidate: str) -> None:
    """In the caller's repository, read-only and before anything is fetched anywhere: the
    candidate must be one commit directly on the base. A candidate built on top of a hidden
    test commit is refused here, so it can never bring that commit into a store Cairnwork
    checks (and so cannot make the commit look already seen)."""
    try:
        parents = run_git(repo, "rev-list", "--parents", "-n", "1", candidate).split()[1:]
    except GitCommandError as exc:
        raise WorkspaceError(f"cannot read {candidate} in {repo}: {exc}") from exc
    if parents != [base]:
        raise CandidateRejected("NOT_ONE_COMMIT_ON_BASE", f"parents of {candidate} are {parents}")


def _absolute(url: str, source: Path) -> str:
    """A relative origin (../upstream.git) means relative to the source repository; the
    scratch clone lives elsewhere, so give it the absolute path."""
    if "://" in url or os.path.isabs(url) or ":" in url.split("/")[0]:
        return url  # a URL, an absolute path, or scp-style host:path
    return str((source / url).resolve())


def _burn(intake: Intake, test_commit: str, *, record: bool) -> None:
    """The deferred tests stay hidden from whoever supplies candidates: a verify run that
    uses them runs candidate code over them and returns its logs (and even pass or fail
    tells a lot), so a test commit is single-use, as on the worker path. Using it is
    registered by creating ``<state>/verify-used-tests/<sha>`` exclusively, before the tests
    are fetched and so before any candidate code runs; a crash or a later refusal still
    counts as used. record=False only checks."""
    marker = _used(intake, test_commit)
    marker.parent.mkdir(mode=0o700, exist_ok=True)
    try:
        if record:
            os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600))
        elif marker.exists():
            raise FileExistsError
    except FileExistsError:
        raise TestCommitRejected("TEST_COMMIT_ALREADY_EXPOSED", "push a new test commit") from None


def tests_consumed(intake: Intake, test_commit: str) -> bool:
    """Is ``test_commit`` now marked used, so a retry needs a new one (#234)? Unknown: True."""
    try:
        return bool(OID.fullmatch(test_commit)) and _used(intake, test_commit).exists()
    except OSError:
        return True


def _used(intake: Intake, test_commit: str) -> Path:
    return intake.state / "verify-used-tests" / test_commit  # checked to be 40 hex digits


def _judge(intake, task, source, specs, candidate, notary, result, verifying) -> dict | None:
    run_root = intake.run_root
    _check_parent(task.repo, task.base_commit, candidate)
    if task.test_commit:
        never_seen(source, task.test_commit)  # the object store the worker path checks
        _burn(intake, task.test_commit, record=False)
    with _scratch(intake, source, task.repo, task, candidate, result.attempt_id) as scratch:
        task = replace(task, repo=scratch)
        return _judge_in(intake, task, specs, candidate, notary, run_root, result, verifying)
    return None  # pragma: no cover - _scratch always yields or raises


def _judge_in(intake, task, specs, candidate, notary, run_root, result, verifying) -> dict | None:
    check_roots_on_base(task.repo, task.base_commit, task.test_roots)
    clone = create_attempt_clone(task.repo, candidate, run_root, result.attempt_id)
    try:
        attempt = Attempt(clone.attempt_id, task.base_commit, clone.path, clone.refs_before)
        check_candidate_commit(task.repo, attempt, candidate, task.allowed_files)
        judged, test_files, binding = candidate, (), None
        if task.test_commit:
            _burn(intake, task.test_commit, record=True)  # before the tests are fetched
            mode = "bundle" if task.test_store and task.test_store.required else "auto"
            # the same journal and row the worker path writes; cairnwork-attest reads it by
            # (task_id, attempt_id), both in this run's output
            intake.journal.record(result.task_id, result.attempt_id, "TEST_SOURCE", mode)
            with base_stage(*verifying):  # base runs: an account failure is no evidence (#234)
                test_files = tests_with_teeth(task, specs, run_root, result, verifying)
            judged = lay_tests_over(
                task.repo, judged, task.test_commit, test_files, result.attempt_id, run_root
            )
            binding = _binding(task, candidate, test_files, judged)
        with base_stage(*verifying):
            plan = p2p_for(task, specs, test_files, run_root, result, verifying)
        result.candidate_commit = judged
        digest = request_digest(task.request() | {"candidate": candidate})
        if binding:  # what a receiver can recompute, unlike the scratch paths in the request
            digest = binding_hash(binding)
        judge_candidate(
            task, specs, digest, notary, run_root, result, test_files, verifying, lambda _: None,
            plan, binding,
        )  # fmt: skip
        return binding
    finally:
        try:
            remove_attempt_clone(run_root, result.attempt_id)
        except WorkspaceError as exc:  # keep the run's own outcome; note the leftover
            result.logs.append(f"cleanup failed: {exc}")


def _binding(task, head: str, test_files: tuple[str, ...], judged: str) -> dict[str, Any]:
    """#229: what the receipt is bound to. The scratch commit that carried the tests dies with
    the scratch clone and its id cannot be rebuilt (commit time), so the receipt names the PR head,
    the tests and the tree that ran, which a receiver rebuilds with deferred_tests.overlay_tree."""
    tree = run_git(task.repo, "rev-parse", f"{judged}^{{tree}}").strip()
    return {
        "rule": OVERLAY_RULE, "base": task.base_commit, "head": head,
        "test_commit": task.test_commit, "test_paths": sorted(test_files), "executed_tree": tree,
        "verifiers": [[verifier_id, list(argv)] for verifier_id, argv in task.verifiers],
    }  # fmt: skip


def _gap() -> None:
    """Between opening a marker and locking it; tests widen it to show the race is closed."""


def _same_file(fd: int, path: Path) -> bool:
    try:
        now, held = os.stat(path, follow_symlinks=False), os.fstat(fd)
    except FileNotFoundError:
        return False
    return (now.st_dev, now.st_ino) == (held.st_dev, held.st_ino)


@contextmanager
def _marked(intake: Intake, attempt_id: str) -> Iterator[None]:
    """Hold a locked marker file for the duration of a run. The lock is taken before the
    marker counts, and only if the path still names the file we locked (a cleaner may
    have removed it in between): otherwise try again."""
    path = intake.state / "verify-runs" / attempt_id
    path.parent.mkdir(mode=0o700, exist_ok=True)
    while True:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        _gap()
        fcntl.flock(fd, fcntl.LOCK_EX)
        if _same_file(fd, path):
            break
        os.close(fd)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)
        os.close(fd)


def cleanup(intake: Intake) -> dict[str, Any]:
    """Remove what runs that died left behind: their orphan processes, clones, scratch
    clones and baseline directories. A run that is still alive holds its marker's lock and
    is left alone; a marker is only removed by whoever holds its lock and still sees the
    same file at that path."""
    markers = intake.state / "verify-runs"
    cleaned: list[str] = []
    for marker in sorted(markers.iterdir()) if markers.is_dir() else []:
        if marker.is_symlink() or not ATTEMPT_ID.fullmatch(marker.name):
            continue
        try:
            fd = os.open(marker, os.O_RDWR | os.O_NOFOLLOW)
        except FileNotFoundError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _gap()
            if not _same_file(fd, marker):
                continue  # replaced by a run that started meanwhile
            sweep_marked(ATTEMPT_ENV, marker.name)
            _remove_leftovers(intake.run_root, marker.name)
            shutil.rmtree(intake.state / "verify-scratch" / marker.name, ignore_errors=True)
            marker.unlink(missing_ok=True)
            cleaned.append(marker.name)
        except BlockingIOError:
            pass  # alive: it holds the lock
        finally:
            os.close(fd)
    return {"cleaned": cleaned}
