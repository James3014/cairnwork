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
Seatbelt's allow-list). A candidate can therefore read an unlisted plaintext secret the
account owns and copy it into the retained verifier log, so **never hand a raw verifier log to
a worker or a model** (`src/cairnwork/verifiers.py` says the same). Other users' `0700` home
directories are protected by ordinary POSIX permissions.

Two consequences of the whole-root read-only bind are mitigated, not fully closed, before P4:

- **Pathname sockets.** A read-only bind does not stop `connect()` to an `AF_UNIX` pathname
  socket, and the network namespace covers only abstract sockets. A reachable root-equivalent
  daemon socket (Docker/Podman/containerd) would hand the candidate the host. Those sockets are
  replaced with `/dev/null` (`_DANGEROUS_SOCKETS`). The general pathname-socket surface — any
  other service the account can reach over a filesystem socket — closes only with P4's own
  account. The SSH agent is already out of reach: the candidate environment is scrubbed to
  `PATH`/`LANG`/`LC_ALL` (`_ENV_KEEP`), so it carries no `SSH_AUTH_SOCK`.
- So "writes and network are confined" holds for ordinary filesystem writes and IP networking;
  a filesystem socket to a privileged local service is the residual the daemon-socket mask and
  P4 address.

CI on `ubuntu-24.04` lifts the AppArmor `apparmor_restrict_unprivileged_userns` restriction
with sysctl; an end user's machine needs an AppArmor profile that grants `userns` to bwrap.

### Processes that escape the run's group

A candidate test can spawn a child that leaves the run's process group (`start_new_session`),
trying to outlive the verdict and do work after it. Both backends stop that, by different means:

- **Seatbelt** cannot reap an escaped session, so `confined.py` sweeps the host process table
  for a process carrying the run token (`CAIRNWORK_VERIFIER_RUN`) outside the run's group, kills
  it, and counts it as a survivor — **a surviving escapee fails the run**, so a worker that
  leaves a process behind gets no passing verdict.
- **bwrap** runs inside an unshared PID namespace (`--unshare-all` implies `--unshare-pid`),
  where bwrap itself is init (PID 1) and the verifier its child. `confined.py` SIGKILLs the
  whole bwrap process group after the verifier exits; killing bwrap (PID 1) tears the namespace
  down and the kernel SIGKILLs every process still in it — the escapee included, since a
  `setsid` child stays in the same PID namespace (leaving one needs `CAP_SYS_ADMIN`, which
  `--cap-drop ALL` + `--disable-userns` deny). So the escapee is already dead before
  `_sweep_survivors` runs — nothing is left to sweep and the run may pass. Containment is by
  construction, not detected after the fact; it simply does not surface as a failed run. Making
  bwrap also fail the run for symmetry is the P3 reaper follow-up, not a security gap.

The invariant both enforce is that the escapee does no work. The witness test
(`test_a_process_that_leaves_the_group_is_contained`) asserts its marker stays absent; note
that because the marker path is outside the write roots, on bwrap the read-only root alone
already forbids the write, so that assertion does not by itself exercise the PID-namespace kill
(the separate write-outside and no-network witnesses, plus the reasoning above, cover it). A
dedicated witness that proves the kill directly is a P3 follow-up.
