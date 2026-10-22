#!/usr/bin/env python3
"""Environment smoke test for onboarding an external repo to Cairnwork.

The slow part of trying Cairnwork on a new repo is not the AI task; it is finding
the environment couplings (writes next to the repo, git safe.directory, the venv's
python not on PATH, network) one at a time, each after a dispatched run fails. This
lists them all up front: run a repo's own already-green tests twice — once bare, and
once through Cairnwork's real verification path — and compare.

Accuracy comes from running the sandbox side through ``verifiers.run_verifiers``
itself (not a hand-rolled profile), so this measures exactly what verification does
and stays correct as that path changes (#33/#36/#40). ``account=None`` means no
VerifierAccount exclusive lock, so probes for different repos run in parallel on one
host. Only pytest repos are covered, because ``parse_verifier`` accepts only
``python3 -m pytest … tests/*.py`` (node/conda come later).

A repo is ENV_OK when its green tests stay green under the sandbox, ENV_COUPLING when
they pass bare but fail under the sandbox (the reason is classified from the verifier
log), and REPO_TESTS_NOT_GREEN when they do not pass bare — in which case the repo
cannot serve as an environment probe at all. A run that passes except for skipped tests is
SKIPPED_TESTS: the verifier rejects skips, so the target must name tests that run on this host.

Scope and honest limits:
- This probes the ``account=None`` path (same uid as the caller). Couplings that only
  appear when verification runs under a separate ``VerifierAccount`` (a different uid) —
  notably git ``safe.directory`` ownership and file-permission handoffs — are NOT covered
  here and can still surface on a real dispatch. A cross-uid probe is route (b), deferred.
- The bare run happens first and in no sandbox, so a test that writes a global resource
  (a fixed host port, ``/tmp/app.sock``, a shared cache) can leave state the sandbox run
  then reuses; and parallel probes can contend on such fixed resources. Keep tests
  hermetic, or lower ``workers`` / run serially when they are not.
"""

from __future__ import annotations

import concurrent.futures as cf
import contextlib
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cairnwork.verifiers import parse_verifier, run_verifiers
from cairnwork.workspace import create_attempt_clone, remove_attempt_clone

# Ordered signatures for classifying a sandbox-only failure from the verifier log.
_SIGNS = (
    ("WRITE_OUTSIDE_REPO", re.compile(r"deny file-write|Operation not permitted|Read-only file")),
    (
        "GIT",
        re.compile(r"dubious ownership|safe\.directory|could not read Username|not a git repo"),
    ),
    ("PATH", re.compile(r"command not found|No such file or directory: '(?:python|git|node)")),
    ("NETWORK", re.compile(r"Could not resolve host|Network is unreachable|deny network")),
)


@dataclass(frozen=True)
class RepoProbe:
    repo: Path  # a git repo
    targets: tuple[str, ...]  # that repo's own green tests, e.g. ("tests/test_a.py",)
    python: str  # absolute path to the venv python that runs those tests


@dataclass(frozen=True)
class ProbeResult:
    repo: str
    # ENV_OK | ENV_COUPLING | SKIPPED_TESTS | REPO_TESTS_NOT_GREEN | SKIP_BARE_TIMEOUT
    # | MIRROR_MULTI_BRANCH
    verdict: str
    reason: str


def _head(repo: Path) -> str:
    out = subprocess.run(  # noqa: S603 - fixed argv
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def _bare_is_green(probe: RepoProbe, commit: str, run_root: Path, timeout: int) -> bool | None:
    """Run the repo's tests with no sandbox in a clean checkout. None if it times out.

    Runs in its own session and, on timeout, kills the whole process group, so a test that
    spawns a background process does not leave an orphan holding the checkout open."""
    clone_id = f"bare-{secrets.token_hex(6)}"
    checkout = create_attempt_clone(probe.repo, commit, run_root, clone_id)
    home = run_root / f"home-{secrets.token_hex(6)}"
    home.mkdir()
    env = {k: os.environ[k] for k in ("LANG", "LC_ALL") if k in os.environ}
    # Match the sandbox side (verifiers #33): the venv's bin leads PATH, so a test that spawns
    # plain ``python`` gets the same interpreter here as under verification. Without this a repo
    # that shells out to ``python`` fails bare and is misreported REPO_TESTS_NOT_GREEN.
    env["PATH"] = os.pathsep.join(
        filter(None, (str(Path(probe.python).parent), os.environ.get("PATH")))
    )
    env.update(HOME=str(home), TMPDIR=str(home), PYTHONDONTWRITEBYTECODE="1")
    argv = [probe.python, "-m", "pytest", "-q", *probe.targets]
    process = subprocess.Popen(  # noqa: S603 - argv from parse_verifier shape, no shell
        argv,
        cwd=checkout.path,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        process.wait(timeout=timeout)
        return process.returncode == 0
    except subprocess.TimeoutExpired:
        return None
    finally:
        # Kill the group by the leader's pid (== pgid under start_new_session), not os.getpgid:
        # after a normal wait() the leader is already reaped, so getpgid would raise and a child
        # the test backgrounded would survive. killpg(pid) still reaches the group while a member
        # lives; suppress only the case where the whole group is already gone.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        remove_attempt_clone(run_root, clone_id)


def _mirror_multi_branch_reason(repo: Path) -> str | None:
    """A reason if repo is a mirror whose origin is not single-branch (Issue #76), else None.
    A repo with no origin fetch refspec (a local probe checkout) is not applicable."""
    got = subprocess.run(  # noqa: S603, S607 - fixed argv, no shell
        ["git", "-C", str(repo), "config", "--get-all", "remote.origin.fetch"],
        capture_output=True,
        text=True,
        check=False,
    )
    specs = [s for s in got.stdout.splitlines() if s.strip()]
    if not specs:
        return None
    # The source of "[+]<src>:<dst>" must be exactly main; a wildcard or another branch is refused.
    if any(spec.lstrip("+").split(":", 1)[0] != "refs/heads/main" for spec in specs):
        return (
            "mirror not single-branch; re-clone with: git clone --single-branch --branch main <url>"
        )
    return None


def probe_repo(probe: RepoProbe, *, timeout: int = 300) -> ProbeResult:
    spec = parse_verifier("env-smoke", ["python3", "-m", "pytest", "-q", *probe.targets])
    name = probe.repo.name
    multi = _mirror_multi_branch_reason(probe.repo)
    if multi is not None:  # a full mirror pre-fetched the hidden-test branches (Issue #76)
        return ProbeResult(name, "MIRROR_MULTI_BRANCH", multi)
    commit = _head(probe.repo)
    # resolve(): on macOS mkdtemp returns /var/folders/... (a symlink to /private/var/...); the
    # sandbox profile uses the realpath, so the checkout must too or pytest collection walks the
    # symlink and is denied at /private.
    run_root = Path(tempfile.mkdtemp(prefix="envsmoke.")).resolve()
    try:
        bare = _bare_is_green(probe, commit, run_root, timeout)
        if bare is None:
            return ProbeResult(name, "SKIP_BARE_TIMEOUT", f"bare tests exceeded {timeout}s")
        if not bare:
            return ProbeResult(name, "REPO_TESTS_NOT_GREEN", "tests do not pass bare; not a probe")
        run = run_verifiers(
            probe.repo,
            commit,
            (spec,),
            run_root,
            "env-smoke",
            python=probe.python,
            timeout=timeout,
            account=None,
            base_commit=commit,
        )[0]
        if run.observation.passed:
            return ProbeResult(name, "ENV_OK", "")
        skipped = _skip_like(run)
        if skipped is not None:
            return ProbeResult(
                name, "SKIPPED_TESTS", f"{skipped} skipped; pick targets without skips"
            )
        return ProbeResult(name, "ENV_COUPLING", _classify(run))
    finally:
        _scrub(run_root)


def _skip_like(run) -> int | None:
    """Count skip-like outcomes when that is the only reason the run was not passed.

    The verifier counts a run as passed only when every collected test is ``passed`` — a
    deliberate rule so a candidate cannot hide a failure behind a skip. So an all-pass-but-skipped
    run (exit 0, nothing mutated, not timed out) is not an environment coupling: the verification
    target just names tests that do not run on this host. Report it as SKIPPED_TESTS so the operator
    picks targets without skips, rather than ``ENV_COUPLING sandbox returncode=0`` which reads like
    an environment problem."""
    if run.timed_out or run.mutated or run.returncode != 0:
        return None
    skip = {"skipped", "xfailed", "xpassed"}
    outcomes = list(run.outcomes.values())
    skipped = sum(1 for outcome in outcomes if outcome in skip)
    if skipped and all(outcome == "passed" or outcome in skip for outcome in outcomes):
        return skipped
    return None


def _classify(run) -> str:
    # Heuristic from the verifier log: WRITE_OUTSIDE_REPO also matches a bare "Operation not
    # permitted" from git/xcrun denials; the order is a best guess, not a proof of cause.
    if run.timed_out:
        return "TIMEOUT"
    if run.mutated:
        return "WRITE_INTO_REPO:" + ",".join(run.mutated[:3])
    try:
        text = run.log_path.read_text(errors="replace")[-20000:]
    except OSError:
        text = ""
    hits = [name for name, pattern in _SIGNS if pattern.search(text)]
    return ",".join(hits) if hits else f"sandbox returncode={run.returncode}"


def probe_all(probes: list[RepoProbe], *, workers: int = 4) -> list[ProbeResult]:
    """account=None => no exclusive verifier lock => probes run in parallel on one host."""
    if not probes:
        return []
    with cf.ThreadPoolExecutor(max_workers=min(workers, len(probes))) as pool:
        return list(pool.map(probe_repo, probes))


def _scrub(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def main(argv: list[str]) -> int:
    # Each argument: <repo>::<python>::<tests/a.py,tests/b.py>
    if not argv:
        print("usage: env_smoke.py <repo>::<python>::<tests/a.py,tests/b.py> ...", file=sys.stderr)
        return 2  # no probes is a usage error, not a clean "everything OK"
    probes = []
    for item in argv:
        repo, python, targets = item.split("::", 2)
        probes.append(RepoProbe(Path(repo), tuple(targets.split(",")), python))
    results = probe_all(probes)
    print(f"{'verdict':<22}{'reason':<34}repo")
    for r in results:
        print(f"{r.verdict:<22}{r.reason:<34}{r.repo}")
    if any(r.verdict == "ENV_COUPLING" for r in results):
        return 1
    # Distinct code: a repo we could not probe at all (not green bare, or bare timed out) is not
    # the same signal as a coupling, but still not a clean onboarding.
    if any(
        r.verdict
        in ("SKIPPED_TESTS", "REPO_TESTS_NOT_GREEN", "SKIP_BARE_TIMEOUT", "MIRROR_MULTI_BRANCH")
        for r in results
    ):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
