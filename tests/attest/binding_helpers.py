"""Build v0.2 (#229) receipts the way verify_candidate does, with a fake Core and no sandbox.

A producer repository holds base, a head (one commit on base) and a test commit (one commit on
base); the executed tree is head with the tests laid over it, carried by a scratch commit only the
producer has. Receivers are fresh repositories that fetch only what they are given.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from cairnwork.core_request import AcceptanceSpec, VerifierObservation
from cairnwork.deferred_tests import OVERLAY_RULE, overlay_tree
from cairnwork.notary import Notary
from cairnwork.notary_verify import binding_hash
from cairnwork_attest.bundle import write_bundle
from cairnwork_attest.dsse import _sign_bytes, sign_statement
from cairnwork_attest.keys import generate_keypair, keyid, load_private_key, public_key_pem
from cairnwork_attest.record import _digest, verify_record
from cairnwork_attest.statement import Producer, statement_bytes
from tests.attest.test_verify import CORE_IDENTITY, fake_core_verify

VERIFIERS = [["pytest-0", ["python3", "-m", "pytest", "-q", "tests"]]]
TESTS = {  # one test file added, one changed
    "tests/test_new.py": "def test_new():\n    pass\n",
    "tests/test_a.py": "def test_a():\n    assert 1\n",
}


def git(repo: Path, *args: str, stdin: str | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], input=stdin, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit_on(repo: Path, parent: str, files: dict[str, str], message: str) -> str:
    git(repo, "checkout", "-q", "--detach", parent)
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "branch", f"b-{sha[:10]}", sha)
    return sha


def producer(tmp_path: Path) -> dict[str, Any]:
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "config", "user.email", "t@e")
    git(work, "config", "user.name", "t")
    (work / "src").mkdir()
    (work / "src/a.py").write_text("x = 1\n")
    (work / "tests").mkdir()
    (work / "tests/test_a.py").write_text("def test_a():\n    pass\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "base")
    base = git(work, "rev-parse", "HEAD")
    head = commit_on(work, base, {"src/a.py": "x = 2\n"}, "fix")
    other = commit_on(work, base, {"src/a.py": "x = 3\n"}, "other fix")
    tests = commit_on(work, base, TESTS, "tests")
    return {"work": work, "base": base, "head": head, "other": other, "tests": tests}


def executed(work: Path, head: str, tests: str, paths: tuple[str, ...]) -> tuple[str, str]:
    """The executed tree (overlay-v1) and a scratch commit carrying it, as lay_tests_over does."""
    with tempfile.TemporaryDirectory() as tmp:
        tree = overlay_tree(work, head, tests, paths, Path(tmp) / "index")
    judged = git(work, "commit-tree", tree, "-p", head, "-m", "scratch")
    return tree, judged


def notarize_bound(
    state: Path, p: dict[str, Any], *, attempt: str = "a1", head: str | None = None,
    overlay_head: str | None = None, paths: tuple[str, ...] | None = None,
    rule: str = OVERLAY_RULE, tree_and_judged: tuple[str, str] | None = None,
) -> dict[str, Any]:  # fmt: skip
    """Notarize one external-candidate run; head is what the binding claims, overlay_head what
    the tests were laid over (they differ only in negative controls)."""
    claimed: str = head or p["head"]
    paths = paths or tuple(sorted(TESTS))
    tree, judged = tree_and_judged or executed(
        p["work"], overlay_head or claimed, p["tests"], paths
    )
    binding = {
        "rule": rule, "base": p["base"], "head": claimed, "test_commit": p["tests"],
        "test_paths": sorted(paths), "executed_tree": tree, "verifiers": VERIFIERS,
    }  # fmt: skip
    task = "verify-0123456789abcdef"
    spec = AcceptanceSpec(task, binding_hash(binding), ("pytest-0",), ("src/a.py", *paths))
    obs = (VerifierObservation("pytest-0", "artifact-1", "sha256:" + "e" * 64, True),)
    state.mkdir(parents=True, exist_ok=True)
    notary = Notary(
        state / "notary.sqlite", core_verify=fake_core_verify, core_identity=CORE_IDENTITY
    )
    verdict = notary.notarize(task, attempt, spec, p["work"], p["base"], judged, obs, binding)
    return {"task": task, "attempt": attempt, "binding": binding, "verdict": verdict,
            "judged": judged}  # fmt: skip


def sign_bound(state: Path, run: dict[str, Any], out: Path) -> Path:
    if not (state / "attest_ed25519.pem").exists():
        generate_keypair(state)
        pem = public_key_pem(load_private_key(state).public_key())
        (state / "public.pem").write_text(pem, encoding="ascii")
    verified = verify_record(
        state / "notary.sqlite", run["task"], run["attempt"],
        expected_candidate=run["binding"]["head"], core_identity=CORE_IDENTITY,
    )  # fmt: skip
    envelope = sign_statement(
        verified, producer=Producer(), timestamp="2026-10-24T00:00:00Z",
        private_key=load_private_key(state), test_source="auto",
    )  # fmt: skip
    write_bundle(out, envelope)
    return out


def resign(state: Path, statement: dict[str, Any], out: Path, *, rehash: bool = False) -> Path:
    """Sign an edited statement with the producer's own key (a buggy or lying signer)."""
    if rehash:
        statement["predicate"]["request_hash"] = _digest(statement["predicate"]["request"])
    write_bundle(out, _sign_bytes(statement_bytes(statement), load_private_key(state)))
    return out


def receiver(tmp_path: Path, name: str, work: Path, *shas: str, depth: int = 0) -> Path:
    """A fresh repository holding only shas (and, unless depth, their history)."""
    repo = tmp_path / name
    repo.mkdir()
    git(repo, "init", "-q")
    for sha in shas:
        extra = ["--depth", str(depth)] if depth else []
        git(repo, "fetch", "-q", *extra, str(work), f"b-{sha[:10]}")
    return repo


def policy(state: Path, tmp_path: Path, test_commit: str, **override: Any) -> dict[str, Any]:
    value = {
        "rule": OVERLAY_RULE, "test_commit": test_commit, "core_identity": CORE_IDENTITY,
        "key_id": keyid(load_private_key(state).public_key()),
    }  # fmt: skip
    value.update(override)
    (tmp_path / "policy.json").write_text(json.dumps(value), encoding="utf-8")
    return value
