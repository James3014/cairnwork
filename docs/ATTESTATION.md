# Cairnwork Change Attestation (Phase 1)

Cairnwork provides automated attestation of verified code changes by converting
its append-only notary records into in-toto Statement v1 documents.

## Verification Gate and Integrity Checks

A Statement is produced only after re-running the notary's verification rules and
confirming that all integrity invariants hold:
1. HTTP status code returned by Core is 200.
2. Verification status reported by Core is `"VERIFIED"`.
3. Integrity flag reported by Core is `"VALID"`.
4. Response echoed hashes match the four request component hashes (`acceptance_contract_hash`, `change_set_hash`, `verification_plan_hash`, `evidence_bundle_hash`).
5. Core identity in the response matches the expected Core identity.
6. Notary database row columns (`task_id`, `attempt_id`, `base_commit`, `candidate_commit`) match the bound request's `contract_id`, `change_set_id`, `source_revision`, and `target_revision`.
7. Candidate commit is supplied by the caller and compared independently against the record's candidate commit and notary verdict.
8. Recomputed `request_hash`: Must equal `"sha256:" + sha256(canonical_json(request))` as computed by the notary.
9. Recomputed commit bindings: The record's `candidate_commit` must equal `change_set.target_revision` (stripped of `git-commit:` prefix) and `base_commit` must equal `change_set.source_revision` (stripped of `git-commit:` prefix).

If any check fails or the notary verdict is not verified, statement creation is refused
with `RecordNotVerified` or `RecordMalformed`.

## In-Process Trust Boundary

The opaque type and re-checks defend against accidental misuse and against callers that only hold the public API; they do not defend against code running in the same process that bypasses them (`object.__setattr__`, direct SQLite writes); the signed envelope (next PR) binds the Statement bytes, and offline verification recomputes everything from the notary database and git objects.

## Statement Structure

An attestation statement is an unsigned in-toto v1 Statement containing:
- `_type`: `"https://in-toto.io/Statement/v1"`.
- `subject`: Identifies the candidate git revision under evaluation (exactly one entry):
  - `git-commit:<candidate>`: Target revision under evaluation.
- `predicateType`: Provisional type URI
  `https://cairnwork.github.io/attestation/verified-change/v0.1`.
- `predicate`: A dictionary binding the execution outcome and provenance:
  - `base_commit`: Base revision against which the change was produced.
  - `request`: The complete generic verification request payload verbatim.
  - `response`: The complete Core verification response verbatim.
  - `request_hash`: The SHA-256 digest of canonical request JSON.
  - `http_status`: HTTP status code returned by the Core verifier (e.g., 200).
  - `status`: Verification verdict status (e.g., `"VERIFIED"`).
  - `integrity`: Integrity flag from Core (e.g., `"VALID"`).
  - `core_identity`: Reported Core identity from the verification response.
  - `verifier_observations`: Exact list of observations from the evidence bundle.
  - `hidden_test_commit`: Null in Phase 1 (not carried in the notary record).
  - `producer`: Software identity and version generating the statement.
  - `recorded_at`: Timestamp when the notary recorded the verdict in SQLite.
  - `timestamp`: Caller-supplied UTC ISO-8601 generation timestamp.
  - `assurance`: Posture and isolation metadata.

## Assurance Semantics

The `assurance` object records the security boundaries and isolation posture
present during candidate evaluation. In Phase 1, typed request fields proving
isolation or execution posture do not exist in the recorded request payload,
so all five fields render `"unknown"`:
- `sandbox_backend`: `"unknown"` (no typed request field proves sandbox backend).
- `verifier_account_isolated`: `"unknown"` (no typed request field proves account isolation).
- `worker_isolated`: `"unknown"` (no typed request field proves worker isolation).
- `hidden_tests`: `"unknown"` (no typed request field proves hidden test evaluation).
- `network`: `"unknown"` (no typed request field proves network isolation posture).

Semantics rule: `"unknown"` means not evidenced. Boolean and enum fields default to
`"unknown"` unless positive or negative proof exists in direct typed request evidence.
Cairnwork never assumes or hard-codes `True` without direct evidence, and does
not accept unproven caller assertions.

- Core identity at re-verification: `verify_record` re-runs the notary's response binding against the Core identity of the *currently installed* `nexus-certify` unless the caller passes `core_identity=` explicitly. A record notarized under an older Core therefore reads as not verified (fail closed) until the caller supplies that Core's identity; the recorded response's own `core_identity` is carried in the predicate for that purpose.

## Signing (DSSE, Ed25519)

A Statement is signed by wrapping its canonical bytes (`statement_bytes`) in a
[DSSE v1](https://github.com/secure-systems-lab/dsse/blob/master/protocol.md)
envelope (`cairnwork_attest.dsse`):

```json
{
  "payloadType": "application/vnd.in-toto+json",
  "payload": "<base64 of the statement bytes>",
  "signatures": [{"keyid": "<sha256 hex of the raw public key>", "sig": "<base64>"}]
}
```

- Public signing is performed via `sign_statement(verified: VerifiedRecord, *, producer: Producer, timestamp: str, private_key: Ed25519PrivateKey) -> dict`: it accepts only an in-process `VerifiedRecord`, builds the Statement via `build_statement`, serialises it with `statement_bytes`, and signs it. The raw-bytes signer is private (`_sign_bytes`).
- The Ed25519 signature covers the DSSE Pre-Authentication Encoding
  `PAE(payloadType, payload)` =
  `"DSSEv1" SP len(type) SP type SP len(payload) SP payload`, never the base64
  text or the JSON envelope, so the payload type is bound by the signature.
- `keyid` is SHA-256 (hex) of the 32-byte raw public key. It is only a hint for
  picking a signature; trust comes from the public key the verifier supplies.
- Multi-signature semantics: DSSE threshold-1 verification is used. The envelope
  verifies when ONE signature whose keyid names the trusted key verifies over PAE;
  other signatures (stale, unrelated, or malformed under other keyids) are ignored.
  `Verification` records `signature_index: int | None` (which entry in `signatures`
  verified). A malformed entry under the TRUSTED keyid still fails closed.
- Strict base64: For the payload and every signature `sig`, standard base64 alphabet
  only is accepted (matching regex `^[A-Za-z0-9+/]*={0,2}$`), decoded with validation,
  and canonical padding is required (`base64.b64encode(decoded) == original.encode()`).
  Urlsafe envelopes and non-canonical padding are refused on purpose (`PAYLOAD_NOT_BASE64`
  / `SIGNATURE_NOT_BASE64`).
- `verify(envelope, public_key)` returns `True` only when a signature whose
  `keyid` names that key verifies over PAE. Everything else is `False`
  (fail-closed); `verify_envelope` returns the same verdict with a reason code
  (`PAYLOAD_TYPE_MISMATCH`, `PAYLOAD_NOT_BASE64`, `SIGNATURES_MISSING`,
  `SIGNATURE_MALFORMED`, `SIGNATURE_NOT_BASE64`, `KEYID_MISMATCH`,
  `BAD_SIGNATURE`, `PUBLIC_KEY_INVALID`, ...) and, only on success, the
  authenticated payload bytes and `signature_index`. Consumers must read the
  Statement from that payload, not from an unverified envelope.

### Keys

`cairnwork_attest.keys` manages one Ed25519 key pair per state directory:

```bash
uv run python -m cairnwork_attest.keys generate --state <state-dir>  # prints keyid + public PEM
uv run python -m cairnwork_attest.keys public --state <state-dir>    # prints them again
```

- The private key is written to `<state-dir>/attest_ed25519.pem` (PKCS#8 PEM,
  unencrypted) with mode `0600`, created exclusively: an existing file or a
  symlink is never overwritten (`KEY_EXISTS`). Key creation is durable: `os.write`
  loops until the entire PEM is written, `os.fsync(fd)` is called before close,
  and the parent directory fd is fsynced after close. On any failure, the path is
  unlinked only if its `st_dev` and `st_ino` match the created file descriptor
  (never deleting a foreign file). The remaining window (a rename of the directory
  itself between create and cleanup) is out of scope.
- Loading avoids check/use races: `load_private_key(state_dir)` opens the key file
  with `O_RDONLY | O_NOFOLLOW` directly, checks `os.fstat(fd)` for a regular file
  (`KEY_NOT_REGULAR`), no group/other permission bits (`KEY_PERMISSIONS_LOOSE`),
  matching owner `st_uid == os.getuid()` (`KEY_WRONG_OWNER`), and reads the PEM
  from that same file descriptor before closing it in `finally`. Missing files
  raise `KEY_NOT_FOUND`. There is no fallback key.
- Private keys never live in the repository or its tests; tests generate keys
  in memory or under a temporary directory.
- Not yet covered: key rotation, revocation, publishing the public key, and
  encrypting the key at rest (the state directory's permissions are the
  boundary).

## Planned Follow-ups (Phase 2+)

The following capabilities and gaps are explicitly deferred from Phase 1:
- signing CLI / offline verify CLI (next PR)
- `hidden_test_commit`: Durably binding the secret evaluation test commit in the notary record.
- Verifier argv and execution log hashes: Explicit digests for verifier invocation commands and execution logs.
- Sandbox profile hash: Not carried in the notary record or evidence bundle.
- Timestamp validation: Timestamp is caller-supplied and not validated as UTC or against a trusted timestamping authority.
- Dedicated CLI and network distribution workflows.
