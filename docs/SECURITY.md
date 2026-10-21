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

### Processes that escape the run's group

A candidate test can spawn a child that leaves the run's process group (`start_new_session`),
trying to outlive the verdict and do work after it. Both backends stop that, by different means:

- **Seatbelt** cannot reap an escaped session, so `confined.py` sweeps the host process table
  for a process carrying the run token (`CAIRNWORK_VERIFIER_RUN`) outside the run's group, kills
  it, and counts it as a survivor — **a surviving escapee fails the run**, so a worker that
  leaves a process behind gets no passing verdict.
- **bwrap** runs the verifier as init (PID 1) of an unshared PID namespace (`--unshare-all`
  implies `--unshare-pid`). When PID 1 exits, the kernel `SIGKILL`s every other process in the
  namespace, so the escapee is already dead before `confined.py` reaps — there is nothing left
  to sweep and the run may pass. Containment is stronger (the escape is killed by construction,
  not detected after the fact); it simply does not surface as a failed run. Making bwrap also
  fail the run for symmetry is the P3 reaper follow-up, not a security gap.

The invariant both enforce, and the one the witness test asserts on either backend, is that the
escapee does no work: it never writes its marker.
