# Security notes

## Verifier sandbox backends

Candidate code runs confined by an OS sandbox; when no working backend exists nothing runs
(there is no unconfined fallback).

- **macOS Seatbelt** (`src/cairnwork_sandbox/seatbelt.py`): default deny. Reads only from an
  allow-list (system libraries, the interpreter, the checkout); writes only to the checkout
  and the per-run directory; no network; credential stores denied last. Behaviour unchanged.
- **Linux bubblewrap** (`src/cairnwork_sandbox/bwrap.py`, P2): a read-only bind of the whole
  root, read-write binds for the write roots only, every namespace unshared (no network), all
  capabilities dropped, nested user namespaces disabled. Named credential directories under
  `$HOME` (`.ssh`, `.aws`, `.gnupg`, `.config`, `.docker`, `.kube`, `.claude`, `.codex`,
  `.gemini`, `.nexus`, `.local/state`) are emptied with a tmpfs and `~/.netrc` is replaced by
  `/dev/null`. Write roots that are `/`, a system or temp directory, `$HOME` or an ancestor of
  `$HOME` are refused.

### Trade-off on Linux (until the P4 dedicated account)

Before the verifier runs under its own account (P4), the bwrap sandbox runs as the account
that launches it. **Everything that account can read is readable inside the sandbox**; only
the named credential list above is masked, so this is not complete read isolation (unlike
Seatbelt's allow-list). Other users' `0700` home directories are protected by ordinary POSIX
permissions. Writes and network are still confined. CI on `ubuntu-24.04` lifts the AppArmor
`apparmor_restrict_unprivileged_userns` restriction with sysctl; an end user's machine needs
an AppArmor profile that grants `userns` to bwrap.
