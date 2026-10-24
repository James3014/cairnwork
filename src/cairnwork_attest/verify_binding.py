"""Offline verification of a v0.2 receipt: an external candidate checked with deferred tests (#229).

The statement's subject is the PR head. Its ``binding`` names the overlay rule, base, head, test
commit and paths, the verifier argv and the tree the verifiers ran (``executed_tree``). Core
judged base -> executed tree, and sha256(binding) is the contract's ``requirements_hash``, which
Core's response echoes through ``acceptance_contract_hash``: a binding cannot be swapped or edited
without breaking that chain (BINDING_HASH_MISMATCH).

Two modes, chosen by whether the receiver holds the test commit:

- full: the tests are here, so the executed tree is rebuilt from head and test commit with the
  overlay rule the verifier used (deferred_tests.overlay_tree) and must equal the bound tree;
- confidential: the tests are not here. A receiver policy is required (POLICY_REQUIRED) and must
  pin this test commit; every non-test path of Core's manifest must equal head's change
  (HEAD_TREE_MISMATCH). The test contents and the overlay are the signer's word, and the result's
  ``assurance`` says so.

The policy is the receiver's own configuration (load_policy), never shipped by the submitter or
read from the receipt. Checks run in a fixed order and the first failure is reported: signature
(verify.py, plus the policy's key id), shape, policy, binding-hash chain, head, base, test source,
tree (full) or head-tree (confidential), Core verdict. Full mode writes the rebuilt tree objects
(trees only, no refs) into the receiver's repository.
"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cairnwork.changeset import ChangeSetError, read_change_set, read_tree_change_set
from cairnwork.core_request import change_manifest
from cairnwork.deferred_tests import OVERLAY_RULE, TestCommitRejected, overlay_tree
from cairnwork.gitcmd import GitCommandError, run_git
from cairnwork.notary_verify import binding_hash
from cairnwork_attest.keys import KeyFileError, keyid, load_public_key_pem
from cairnwork_attest.record import AttestError, _digest, parse_strict_json
from cairnwork_attest.verify_response import VerifyResult, _check_response_verified, _fail
from cairnwork_protocol import acceptance_contract_hash, change_manifest_hash

KNOWN_RULES = (OVERLAY_RULE,)
_BINDING_KEYS = {"rule", "base", "head", "test_commit", "test_paths", "executed_tree", "verifiers"}
_POLICY_KEYS = {"rule", "test_commit", "core_identity", "key_id"}
_OID = re.compile(r"^[0-9a-f]{40}$")
_KEYID = re.compile(r"^[0-9a-f]{64}$")
_REGULAR = ("100644", "100755")


class PolicyError(AttestError):
    code: str = "POLICY_MALFORMED"


def load_policy(path: Path) -> dict[str, Any]:
    """The receiver's policy: strict JSON with exactly ``rule`` (a known overlay rule),
    ``test_commit`` (40 hex), ``core_identity`` (an object) and ``key_id`` (64 hex)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PolicyError(f"cannot read policy: {exc}") from exc
    policy = parse_strict_json(
        text, error_cls=PolicyError, malformed_code="POLICY_MALFORMED",
        constant_code="POLICY_MALFORMED",
    )  # fmt: skip
    problem = _policy_problem(policy)
    if problem:
        raise PolicyError(problem)
    return policy


def _policy_problem(policy: Any) -> str:
    """Why policy is not a valid receiver policy, else "" (load_policy and verify_bound)."""
    if not isinstance(policy, Mapping) or set(policy) != _POLICY_KEYS:
        return f"a policy has exactly the keys {sorted(_POLICY_KEYS)}"
    if (
        policy["rule"] not in KNOWN_RULES
        or not _oid(policy["test_commit"])
        or not isinstance(policy["core_identity"], dict)
        or not (isinstance(policy["key_id"], str) and _KEYID.fullmatch(policy["key_id"]))
    ):
        return "policy values: known rule, 40-hex test_commit, object, 64-hex key_id"
    return ""


def _oid(value: Any) -> bool:
    return isinstance(value, str) and _OID.fullmatch(value) is not None


def _has(repo: Path, spec: str) -> bool:
    try:
        run_git(repo, "cat-file", "-e", spec)
    except GitCommandError:
        return False
    return True


@dataclass(frozen=True)
class _Ctx:
    repo: Path
    statement: dict[str, Any]
    binding: dict[str, Any]
    expected_candidate: str
    expected_base: str | None
    policy: Mapping[str, Any] | None
    full: bool

    @property
    def predicate(self) -> dict[str, Any]:
        return self.statement["predicate"]

    @property
    def request(self) -> dict[str, Any]:
        return self.statement["predicate"]["request"]


def verify_bound(
    statement: dict[str, Any],
    *,
    repo: Path,
    expected_candidate: str,
    public_key_pem: Path,
    core_identity_pin: Mapping[str, Any] | None = None,
    expected_base: str | None = None,
    policy: Mapping[str, Any] | None = None,
) -> VerifyResult:
    """Check a v0.2 statement whose DSSE signature verify.py has already checked."""
    if policy is not None and (problem := _policy_problem(policy)):
        return _fail("POLICY_MALFORMED", problem)  # a caller's dict, not load_policy's
    if policy is not None and _keyid_of(public_key_pem) != policy.get("key_id"):
        return _fail("SIGNATURE_INVALID", "the public key is not the key the policy pins")
    binding, err = _shape(statement)
    if err is not None:  # a failed VerifyResult is falsy: never `err or ...`
        return err
    if binding is None:
        return _fail("STATEMENT_SHAPE")
    full = _has(repo, f"{binding['test_commit']}^{{commit}}")
    if not full and policy is None:
        return _fail("POLICY_REQUIRED", "test commit not here (confidential mode): give a policy")
    ctx = _Ctx(repo, statement, binding, expected_candidate, expected_base, policy, full)
    checks: tuple[Callable[[_Ctx], VerifyResult | None], ...] = (
        _chain, _head, _base, _test_source, _tree if full else _head_tree,
    )  # fmt: skip
    for check in checks:
        err = check(ctx)
        if err is not None:
            return err
    pins = [p for p in ((policy or {}).get("core_identity"), core_identity_pin) if p is not None]
    for pin in pins or [None]:
        err = _check_response_verified(ctx.predicate, pin)
        if err is not None:
            return err
    return VerifyResult(
        ok=True,
        code="OK",
        statement=statement,
        assurance=_assurance(full, pinned=policy is not None),
    )


def _keyid_of(public_key_pem: Path) -> str:
    try:
        return keyid(load_public_key_pem(Path(public_key_pem).read_bytes()))
    except (KeyFileError, OSError):
        return ""


def _shape(statement: dict[str, Any]) -> tuple[dict[str, Any] | None, VerifyResult | None]:
    subjects = statement.get("subject")
    subject = subjects[0] if isinstance(subjects, list) and len(subjects) == 1 else None
    digest = subject.get("digest") if isinstance(subject, dict) else None
    if not isinstance(digest, dict) or not _oid(digest.get("gitCommit")):
        return None, _fail("SUBJECT_SHAPE", "subject must be one entry with a 40-hex gitCommit")
    predicate = statement["predicate"]
    if not _oid(predicate.get("base_commit")):
        return None, _fail("SUBJECT_SHAPE", "predicate.base_commit is not a 40-hex commit")
    if not isinstance(predicate.get("request"), dict) or not isinstance(
        predicate.get("response"), dict
    ):
        return None, _fail("STATEMENT_SHAPE", "predicate.request and response must be objects")
    binding = predicate.get("binding")
    if not isinstance(binding, dict) or set(binding) != _BINDING_KEYS:
        return None, _fail("STATEMENT_SHAPE", f"binding must have exactly {sorted(_BINDING_KEYS)}")
    if binding["rule"] not in KNOWN_RULES:
        return None, _fail("STATEMENT_SHAPE", f"unknown overlay rule {binding['rule']!r}")
    paths, verifiers = binding["test_paths"], binding["verifiers"]
    if (
        not all(_oid(binding[k]) for k in ("base", "head", "test_commit", "executed_tree"))
        or not isinstance(paths, list)
        or not paths
        or not all(isinstance(p, str) for p in paths)
        or paths != sorted(set(paths))
        or not isinstance(verifiers, list)
        or not all(_verifier_shape(v) for v in verifiers)
    ):
        return None, _fail("STATEMENT_SHAPE", "binding values have the wrong shape")
    return binding, None


def _verifier_shape(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], list)
        and all(isinstance(arg, str) for arg in value[1])
    )


def _chain(ctx: _Ctx) -> VerifyResult | None:
    """sha256(binding) == contract requirements_hash == the contract Core's response echoes, and
    the request is for the binding's base and executed tree."""
    request, binding = ctx.request, ctx.binding
    if ctx.predicate.get("request_hash") != _digest(request):
        return _fail("REQUEST_HASH_MISMATCH", "predicate.request_hash is not the request's digest")
    contract, change_set = request.get("acceptance_contract"), request.get("change_set")
    manifest = request.get("change_manifest")
    if not (
        isinstance(contract, dict) and isinstance(change_set, dict) and isinstance(manifest, dict)
    ):
        return _fail("BINDING_HASH_MISMATCH", "request lacks contract, change set or manifest")
    if contract.get("requirements_hash") != binding_hash(binding):
        return _fail("BINDING_HASH_MISMATCH", "requirements_hash is not sha256(binding)")
    hashes = ctx.predicate["response"].get("hashes")
    try:
        echoed = isinstance(hashes, dict) and hashes.get(
            "acceptance_contract_hash"
        ) == acceptance_contract_hash(contract)
    except (KeyError, TypeError, ValueError):
        echoed = False
    if not echoed:
        return _fail("BINDING_HASH_MISMATCH", "Core's response does not echo this contract")
    tree = f"git-tree:{binding['executed_tree']}"
    if change_set.get("target_revision") != tree:
        return _fail("BINDING_HASH_MISMATCH", "change_set.target_revision is not the bound tree")
    if manifest.get("target_tree") != tree:
        return _fail("BINDING_HASH_MISMATCH", "change_manifest.target_tree is not the bound tree")
    if change_set.get("source_revision") != f"git-commit:{binding['base']}":
        return _fail("BINDING_HASH_MISMATCH", "change_set.source_revision is not the bound base")
    return None


def _head(ctx: _Ctx) -> VerifyResult | None:
    subject = ctx.statement["subject"][0]["digest"]["gitCommit"]
    head = ctx.binding["head"]
    if not subject == head == ctx.expected_candidate:
        return _fail(
            "HEAD_MISMATCH",
            f"subject {subject}, binding head {head}, expected {ctx.expected_candidate!r}",
        )
    if not _has(ctx.repo, f"{head}^{{commit}}"):
        return _fail("COMMIT_NOT_FOUND", f"head {head} not found in repo")
    return None


def _base(ctx: _Ctx) -> VerifyResult | None:
    base = ctx.binding["base"]
    if ctx.predicate["base_commit"] != base:
        return _fail("BASE_MISMATCH", f"predicate.base_commit is not the binding's base {base}")
    if ctx.expected_base is not None and ctx.expected_base != base:
        return _fail("BASE_MISMATCH", f"base {base} != expected_base {ctx.expected_base}")
    if not _has(ctx.repo, f"{base}^{{commit}}"):
        return _fail("BASE_NOT_FOUND", f"base {base} not found in repo")
    return None


def _test_source(ctx: _Ctx) -> VerifyResult | None:
    """The policy's pins; in full mode the test commit itself: one commit on the base whose
    changed paths are exactly the bound test paths."""
    binding, test_commit = ctx.binding, ctx.binding["test_commit"]
    if ctx.predicate.get("hidden_test_commit") != test_commit:
        return _fail("TEST_SOURCE_MISMATCH", "hidden_test_commit is not the binding's")
    if ctx.policy is not None and [ctx.policy["test_commit"], ctx.policy["rule"]] != [
        test_commit,
        binding["rule"],
    ]:
        return _fail("TEST_SOURCE_MISMATCH", f"policy pins another test commit than {test_commit}")
    if not ctx.full:
        return None
    try:
        parents = run_git(ctx.repo, "rev-list", "--parents", "-n", "1", test_commit).split()[1:]
        names = run_git(
            ctx.repo, "diff-tree", "-r", "-z", "--no-renames", "--name-only",
            binding["base"], test_commit,
        ).split("\0")  # fmt: skip
    except GitCommandError as exc:
        return _fail("TEST_SOURCE_MISMATCH", f"cannot read the test commit: {exc}")
    if parents != [binding["base"]] or sorted(n for n in names if n) != binding["test_paths"]:
        return _fail("TEST_SOURCE_MISMATCH", "test commit is not one commit of these paths on base")
    return None


def _tree(ctx: _Ctx) -> VerifyResult | None:
    """Full mode: rebuild the executed tree from head and the test commit (overlay-v1); it and
    Core's manifest must be the bound ones."""
    binding = ctx.binding
    with tempfile.TemporaryDirectory() as tmp:
        try:
            tree = overlay_tree(
                ctx.repo, binding["head"], binding["test_commit"], tuple(binding["test_paths"]),
                Path(tmp) / "index",
            )  # fmt: skip
        except TestCommitRejected as exc:  # a git failure here is the receiver's environment
            code = "OVERLAY_UNSUPPORTED_ENTRY" if exc.code == "OVERLAY_UNSUPPORTED_ENTRY" else ""
            return _fail(code or "REBUILD_FAILED", str(exc))
    if tree != binding["executed_tree"]:
        return _fail("TREE_MISMATCH", f"head + tests give {tree}, not {binding['executed_tree']}")
    try:
        manifest = change_manifest(read_tree_change_set(ctx.repo, binding["base"], tree))
    except ChangeSetError as exc:
        return _fail("TREE_MISMATCH", f"cannot read base -> executed tree: {exc}")
    if ctx.request["change_manifest"] != manifest:
        return _fail("TREE_MISMATCH", "Core's manifest is not base -> the rebuilt tree")
    if ctx.request["change_set"].get("diff_hash") != change_manifest_hash(manifest):
        return _fail("TREE_MISMATCH", "change_set.diff_hash is not the rebuilt manifest's hash")
    return None


def _head_tree(ctx: _Ctx) -> VerifyResult | None:
    """Confidential mode: Core's manifest (base -> executed tree) must match base -> head on
    every non-test path, and change each test path to a regular file."""
    binding, manifest = ctx.binding, ctx.request["change_manifest"]
    try:
        head_change = read_change_set(ctx.repo, binding["base"], binding["head"])
    except ChangeSetError as exc:
        return _fail("HEAD_TREE_MISMATCH", f"cannot read base -> head: {exc}")
    entries = manifest.get("entries")
    if manifest.get("source_tree") != f"git-tree:{head_change.base_tree}":
        return _fail("HEAD_TREE_MISMATCH", "Core's manifest does not start at the base tree")
    if not isinstance(entries, list) or not all(
        isinstance(e, dict) and isinstance(e.get("path"), str) for e in entries
    ):
        return _fail("HEAD_TREE_MISMATCH", "Core's manifest entries are malformed")
    claimed = {e.get("path"): e for e in entries}
    heads = {e.path: e.to_core() for e in head_change.entries}
    tests = set(binding["test_paths"])
    if len(claimed) != len(entries):
        return _fail("HEAD_TREE_MISMATCH", "Core's manifest names a path twice")
    for path in sorted(tests):
        entry, parts = claimed.get(path), path.split("/")
        if {"", ".", ".."} & set(parts) or ".git" in {p.casefold() for p in parts}:
            return _fail("OVERLAY_UNSUPPORTED_ENTRY", f"test path {path!r}")
        if entry is None or entry.get("change_type") not in ("ADD", "MODIFY"):
            return _fail("HEAD_TREE_MISMATCH", f"test path {path} is not added or modified")
        if entry.get("after_mode") not in _REGULAR:
            return _fail("OVERLAY_UNSUPPORTED_ENTRY", f"test path {path} is not a regular file")
    for path in sorted((set(claimed) | set(heads)) - tests, key=str):
        if claimed.get(path) != heads.get(path):
            return _fail("HEAD_TREE_MISMATCH", f"{path} in the executed tree is not head's")
    return None


def _assurance(full: bool, *, pinned: bool) -> dict[str, str]:
    """What this receiver checked itself; the verifier runs are always the signer's word. Without
    a policy the test source is whichever test commit the signer chose (``signer_choice``)."""
    return {
        "mode": "full" if full else "confidential",
        "test_source": "policy_pinned" if pinned else "signer_choice",
        "head": "verified",
        "non_test_paths": "verified" if full else "verified_against_core_manifest",
        "test_contents": "verified" if full else "not_verified",
        "overlay": "rebuilt" if full else "not_verified",
        "verifier_runs": "signer_claim",
    }
