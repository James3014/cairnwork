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
cannot serve as an environment probe at all.
"""

from __future__ import annotations

import concurrent.futures as cf
import os
import re
import secrets
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
    verdict: str  # ENV_OK | ENV_COUPLING | REPO_TESTS_NOT_GREEN | SKIP_BARE_TIMEOUT
    reason: str


def _head(repo: Path) -> str:
    out = subprocess.run(  # noqa: S603 - fixed argv
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def _bare_is_green(probe: RepoProbe, commit: str, run_root: Path, timeout: int) -> bool | None:
    """Run the repo's tests with no sandbox in a clean checkout. None if it times out.

    The timeout kills only the direct child; a test that spawns a detached daemon can outlive it.
    That is acceptable for a manual onboarding probe."""
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
    try:
        done = subprocess.run(  # noqa: S603 - argv from parse_verifier shape, no shell
            argv, cwd=checkout.path, env=env, capture_output=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return None
    finally:
        remove_attempt_clone(run_root, clone_id)
    return done.returncode == 0


def probe_repo(probe: RepoProbe, *, timeout: int = 300) -> ProbeResult:
    spec = parse_verifier("env-smoke", ["python3", "-m", "pytest", "-q", *probe.targets])
    commit = _head(probe.repo)
    name = probe.repo.name
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
        return ProbeResult(name, "ENV_COUPLING", _classify(run))
    finally:
        _scrub(run_root)


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
    if any(r.verdict in ("REPO_TESTS_NOT_GREEN", "SKIP_BARE_TIMEOUT") for r in results):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
