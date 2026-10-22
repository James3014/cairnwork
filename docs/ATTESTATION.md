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

## Planned Follow-ups (Phase 2+)

The following capabilities and gaps are explicitly deferred from Phase 1:
- Digital signatures: Cryptographic DSSE or in-toto envelope signing.
- `hidden_test_commit`: Durably binding the secret evaluation test commit in the notary record.
- Verifier argv and execution log hashes: Explicit digests for verifier invocation commands and execution logs.
- Sandbox profile hash: Not carried in the notary record or evidence bundle.
- Timestamp validation: Timestamp is caller-supplied and not validated as UTC or against a trusted timestamping authority.
- Dedicated CLI and network distribution workflows.
