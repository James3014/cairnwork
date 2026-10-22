# Cairnwork Change Attestation (Phase 1)

Cairnwork provides automated attestation of verified code changes by converting
its append-only notary records into in-toto Statement v1 documents.

## Statement Structure

An attestation statement is an unsigned in-toto v1 Statement containing:
- `_type`: `"https://in-toto.io/Statement/v1"`.
- `subject`: Identifies the git revisions involved in the change:
  - `git-commit:<candidate>`: Target revision under evaluation.
  - `git-commit:<base>`: Base revision against which the change was produced.
- `predicateType`: Provisional type URI
  `https://cairnwork.github.io/attestation/verified-change/v0.1`.
- `predicate`: A dictionary binding the execution outcome and provenance:
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
present during candidate evaluation:
- `sandbox_backend`: `"seatbelt"`, `"bubblewrap"`, `"none"`, or `"unknown"`.
- `verifier_account_isolated`: Whether verification ran under a dedicated account.
- `worker_isolated`: Whether the worker environment was isolated.
- `hidden_tests`: Whether evaluation executed hidden tests against the candidate.
- `network`: Network access mode (`"off"`, `"on"`, or `"unknown"`).

Semantics rule: `"unknown"` means not evidenced. Boolean fields default to
`"unknown"` unless positive or negative proof exists in configuration or run
metadata. Cairnwork never assumes or hard-codes `True` without direct evidence.

## Planned Follow-ups (Phase 2+)

The following capabilities are explicitly deferred from Phase 1:
- Digital signatures: Cryptographic DSSE or in-toto envelope signing.
- `hidden_test_commit`: Durably binding the secret evaluation test commit.
- Explicit verifier hashes: Individual argv and execution log digests.
- Dedicated CLI and network distribution workflows.
