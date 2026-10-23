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
  - `test_source`: `"bundle"`, `"auto"` or `"none"`, the hidden-test mode the run itself used
    (the pipeline journals a `TEST_SOURCE` row in `journal.sqlite` before it checks the deferred
    tests; `statement` reads that row read-only by task and attempt, never `config.json`).
    `cairnwork-verify` writes the same row, in the same `journal.sqlite`, under its one-off
    `verify-...` task id (printed as `task_id` next to `attempt_id` in its JSON output); pass
    those two to `cairnwork-attest statement`.
    No row (the task had no hidden tests, or the run predates this change) is `none`.
    Confidentiality of the hidden tests is claimed only for `bundle`; `auto` and `none` make no
    such claim. A config changed after the run does not change the recorded value.
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

- Envelope top-level keys must be strictly the whitelist `payloadType`, `payload`, `signatures`; any extra top-level key fails envelope validation with `ENVELOPE_SHAPE`. Unknown fields inside the signed Statement payload may stay for forward compatibility.
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
  (never deleting a foreign file). Between `_safe_unlink`'s `stat` and `unlink`
  (both via the directory fd) the path can still be replaced by someone who can
  already write into the owner's 0700 key directory; that window is out of scope.
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

## Offline Verification

Offline verification (`cairnwork_attest.verify.verify_bundle`) validates a DSSE
attestation bundle against local git repository objects and a trusted Ed25519
public key. Verification is completely fail-closed and runs with **no network access**.

`verify_bundle` returns a `VerifyResult(ok=bool, code=str, detail=str, statement=dict | None)`
and never raises exceptions for external input or verification failures. Bundle read
errors (missing file, oversize file, non-regular file) return `BUNDLE_UNREADABLE`;
strict-JSON decode failures return `BUNDLE_MALFORMED`; envelope structural problems
return `ENVELOPE_SHAPE`. Only programmer errors (such as wrong argument types) may raise.

### What Is Recomputed vs. What Is Trusted

- **What is trusted**:
  - Only the Ed25519 public key you supply.
  - The local git repository objects (`cat-file` and `diff-tree`).
  - Without a Core identity pin, the signing key vouches for the reported Core identity.
- **What is recomputed and checked** (in order, failing closed at the first violation):
  1. `BUNDLE_UNREADABLE` / `BUNDLE_MALFORMED` / `ENVELOPE_SHAPE` / `SIGNATURE_INVALID`: Bundle envelope is read using a single `os.open(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)` call, verified with `fstat` to be a regular file with `st_size <= MAX` (4 MiB), and at most MAX+1 bytes are read from the fd (refusing if more arrives). Any missing file, oversize file, non-regular file, or read failure returns `BUNDLE_UNREADABLE`. Strict JSON decode failures (including duplicate keys) return `BUNDLE_MALFORMED`. The envelope must decode as strict JSON containing strictly `payloadType`, `payload`, `signatures` (any extra key → `ENVELOPE_SHAPE`), and its DSSE signature verified over `PAE(payloadType, payload)` using the supplied public key (`SIGNATURE_INVALID`).
  2. `STATEMENT_SHAPE`: Statement payload decodes as strict JSON with `_type == "https://in-toto.io/Statement/v1"`, `predicateType == "https://cairnwork.github.io/attestation/verified-change/v0.1"`, and an object `predicate`. Unknown fields inside the signed Statement may stay for forward compatibility.
  3. `SUBJECT_SHAPE`: Statement has exactly one subject entry with a valid 40-hex `gitCommit` digest, and `predicate.base_commit` is a 40-hex commit hash.
  4. `CANDIDATE_MISMATCH`: The Statement's subject `gitCommit` must equal `expected_candidate` (checked right after signature and shape checks). Verifying without pinning the candidate proves only that "some commit was verified".
  5. `COMMIT_NOT_FOUND`: Both candidate and base commits exist in the target git repository (`git cat-file -e <sha>^{commit}`).
  6. `REQUEST_HASH_MISMATCH`: `predicate.request_hash` matches the recomputed SHA-256 digest of canonical JSON `predicate.request` using the notary digest rule.
  7. `COMMIT_BINDING_MISMATCH`: `predicate.request.change_set.target_revision` and `source_revision` match `git-commit:<candidate>` and `git-commit:<base>`.
  8. `MANIFEST_MISMATCH`: The change manifest is recomputed from raw git tree objects between base and candidate commits (`read_change_set`), and its canonical `change_manifest_hash` matches both `predicate.request.change_set.diff_hash` and `cairnwork_protocol.change_manifest_hash(predicate.request.change_manifest)`.
  9. `RESPONSE_NOT_VERIFIED`: The verification response reports status `"VERIFIED"` and integrity `"VALID"` with HTTP 200, echoes all five request hashes (`acceptance_contract_hash`, `change_set_hash`, `verification_plan_hash`, `evidence_bundle_hash`, and required `change_manifest_hash`), satisfies `response_binding_problem` against the response's reported `core_identity`, and matches `predicate.response.core_identity == predicate.core_identity` (both present, deep-equal).
  10. `CORE_IDENTITY_MISMATCH`: When an optional Core identity pin is provided (`--core-identity <json-file>` / `core_identity_pin`), it must match `predicate.response.core_identity`.

### CLI Usage

The `cairnwork-attest` CLI provides key management, statement creation, and offline verification:

```bash
# 1. Key generation: generate an Ed25519 signing keypair into the state directory
cairnwork-attest keygen --state <state-dir>

# 2. Statement creation: read verified notary record and build/sign statement bundle
cairnwork-attest statement --state <state-dir> --task-id <id> --attempt-id <id> --candidate <sha> --out bundle.json --sign

# 3. Offline verification: verify the signed bundle using public key and git repo
cairnwork-attest verify bundle.json --public-key <public-key.pem> --repo <git-repo> --candidate <sha> [--core-identity <json>]
```

`--candidate <sha>` is required with no default. The caller must decide and pass the candidate commit explicitly (for example, `--candidate "$(git rev-parse HEAD)"`). The tool refuses to guess the candidate from HEAD or the local environment because an attacker-controlled checkout must never be trusted to choose the candidate commit under evaluation.

Missing or malformed `--candidate` (not a 40-character hex commit SHA) produces a usage error, prints a message to stderr, and exits 2. The CLI prints which candidate it is checking against (`Candidate: <sha>`). On success, `verify` prints `OK <candidate>` and exits 0. On failure, it prints `<CODE>: <detail>` and exits 1.

`verify --json` also prints `test_source` (`"bundle"`, `"auto"`, `"none"`, or `null` when the bundle
lacks it, e.g. one signed before this field existed). The field is informational: `verify` does
not yet refuse `auto` bundles.

## Publishing to a forge (Phase 2, step 1)

Once a signed bundle exists, `cairnwork-attest publish` delivers it to the forge by attaching it as a git note on a dedicated notes ref and pushing that ref to the remote. Signing and publishing are separate steps by design: `publish` takes an already-signed bundle, never calls Core and never modifies the candidate commit.

```shell
cairnwork-attest publish \
  --bundle attestation.bundle.json \
  --repo . \
  --candidate <sha> \
  --remote origin \
  --notes-ref refs/notes/cairnwork
```

The note stores the bundle bytes exactly (`git hash-object -w` + `git notes add -C`, so no whitespace normalisation). `cairnwork/verify-action` fetches `refs/notes/cairnwork` and reads the note for the candidate commit. `publish` never overwrites an existing note: the same bundle is a no-op (`NOOP`, exit 0), a different bundle for the same candidate is `NOTE_CONFLICT` (exit 1, left for a human). A push rejected as non-fast-forward (another publisher advanced the ref) is retried after re-fetching, up to `--max-attempts`; network or identity failures exit 2. No automatic notes merge strategy is used. The local notes ref is treated as a mirror of the remote: each attempt force-fetches it, so notes that exist only locally on that ref are discarded. `--remote` must be a configured remote name and `--notes-ref` must be under `refs/notes/`; anything else is rejected as `USAGE` before git runs. `--json` prints one line with `ok`, `code`, `detail`, `candidate`, `notes_ref`, `remote`, `attempts`, `blob` and `notes_tip` (the notes ref commit after the push; `null` when the push succeeded but the local tip could not be read, shown as `-` in the plain-text `PUBLISHED` line). When a push fails and the local notes ref cannot be restored, the code is `LOCAL_REF_RESET_FAILED` and `detail` carries both the restore and the push error.

## Check Run (Phase 2, informational)

After publishing, `cairnwork-attest check-run` posts a GitHub Check Run on the candidate commit so reviewers see the attestation facts in the pull request UI:

```shell
CAIRNWORK_GITHUB_TOKEN=... cairnwork-attest check-run \
  --bundle attestation.bundle.json \
  --repo-slug OWNER/NAME \
  --public-key attestation.pub.pem
```

The Check Run lists the candidate, base, SHA-256 of the signed Statement bytes, test source, key id and the offline verification result (with `--public-key` the signature is checked, reported as `VERIFIED, signature only`; add `--repo` to run the full `verify`; `--repo` needs `--public-key`; without a key the title is marked `UNVERIFIED` and the summary says the signature was not checked). A missing or malformed key file fails before anything is posted (`KEY_UNREADABLE` / `KEY_MALFORMED`), so an operator mistake never reads as a forged bundle. Its conclusion is always `neutral`: it is informational and never the enforcing gate. Enforcement stays with `cairnwork/verify-action`, which verifies the published note itself.

The token is read only from the environment variable `CAIRNWORK_GITHUB_TOKEN` (never a command-line argument) and is never printed. Use a GitHub App installation token with `checks: write`. Bundle values enter the summary only when they have the expected shape (40-hex commits, 64-hex key id, a lowercase `test_source` word); anything else is shown as `invalid`, so no free text from the bundle reaches the pull request page. GitHub error messages are redacted of the token and reduced to plain text. Exit 0 means `CREATED`; usage, key, bundle, HTTP and network failures exit 2.

## On Verified (runner hook)

A runner calls `cairnwork-attest on-verified` once a task was verified. It runs `statement --sign`, `publish` and `check-run` in that order and stops at the first failing step (later steps are reported as `SKIPPED`):

```bash
cairnwork-attest on-verified --state /abs/state --task-id T --attempt-id A \
  --candidate <40-hex> --repo /abs/clone --repo-slug OWNER/NAME \
  [--remote origin] [--notes-ref refs/notes/cairnwork] [--out-dir /abs/dir] [--no-check-run]
```

The bundle is written to `<out-dir or STATE/attestations>/<task>-<attempt>.bundle.json` with mode 0600. Task and attempt ids must match `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`; paths must be absolute. A bundle already at that path is reused only if it fully verifies for this candidate (so a re-run publishes `NOOP`); any other file there fails with `BUNDLE_EXISTS`. The Check Run signature check uses the state's own public key. The token comes from `CAIRNWORK_GITHUB_TOKEN`; without it the `check_run` step is `USAGE` (the note is still published).

It prints one JSON line (sorted keys): `ok`, `task_id`, `attempt_id`, `candidate`, `bundle` and `steps` (`step`, `code`, `detail`). Exit 0 when every step that ran succeeded (statement `OK`, publish `PUBLISHED` or `NOOP`, check run `CREATED` or skipped by `--no-check-run`), 1 when publish returned `NOTE_CONFLICT`, 2 for any other failure. The token never appears in the output.

## Runner wiring (opt-in, default off)

The Issue runner can call an executable after a task's PR is open: set the state `config.json` key `post_verified_hook` to an absolute path (and optionally `post_verified_hook_seconds`, default 300). Unset, nothing runs and the runner behaves exactly as before. The intended hook is `cairnwork-attest on-verified`, which signs the statement, publishes the note and posts the informational Check Run; it needs a signing key in the same state directory (`cairnwork-attest keygen --state <state>`). Because the key names one executable (no subcommand or extra arguments), point it at a small wrapper:

```sh
#!/bin/sh
exec /abs/path/to/cairnwork-attest on-verified "$@"
```

Make the wrapper executable (`chmod 700`) and keep it owned by the runner account.

The hook runs only in the verified branch, after the PR, labels and comment are final, so it can never change the outcome. It gets the task, attempt, candidate, clone and `OWNER/NAME` as arguments, and only `PATH`, `HOME`, `LANG`, `LC_ALL`, `CAIRNWORK_GITHUB_TOKEN` (from the runner's own environment) and the git settings that let its notes push authenticate with the same `gh` credential helper as the runner's branch push. Every run is recorded in `<state>/post-verified-hook.jsonl` (token redacted); a failure, timeout or missing executable also prints one `ALERT post_verified_hook ...` line on stderr. The hook runs in its own session, so a timeout kills whatever it started too. The log is only appended to when it is a private (0600, owned by the runner) single-link regular file; anything else is alerted and skipped. The runner's printed result and exit code are exactly what they are with the hook off.

## Planned Follow-ups (Phase 2+)

The following capabilities and gaps are explicitly deferred:
- Sigstore signing and transparency log integration.
- Enforcing forge status (the notes push is `cairnwork-attest publish`; the informational Check Run is `cairnwork-attest check-run`).
- `hidden_test_commit`: Durably binding secret evaluation test commit in the notary record.
- Verifier argv and execution log hashes: Explicit digests for verifier invocation commands and execution logs.
- Sandbox profile hash: Not carried in the notary record or evidence bundle.
- Timestamp validation: Timestamp is caller-supplied and not validated as UTC or against a trusted timestamping authority.
