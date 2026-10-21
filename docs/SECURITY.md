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
  socket, and the network namespace covers only abstract sockets. A reachable privileged local
  socket hands the candidate the host — the container daemons (Docker/Podman/containerd), the
  system D-Bus, and the **systemd user bus** under `/run/user/<uid>` (a `systemd-run --user`
  there starts a process *outside* the sandbox). The daemon and system-bus sockets are replaced
  with `/dev/null` (`_DANGEROUS_SOCKETS`); the whole `/run/user` runtime tree is emptied with a
  tmpfs — every uid's subdir at once, so it holds under a P4 account uid too — which also removes
  the SSH/keyring **agent** socket that lives there.
  An agent socket an operator put at some *other* path is still reachable by a candidate that
  guesses it (scrubbing `SSH_AUTH_SOCK` from the environment hides the usual pointer but not the
  path itself) — this residual, and any privileged socket not in the list above, closes only
  with the P4 dedicated account.
- So "writes and network are confined" holds for ordinary filesystem writes and IP networking;
  a filesystem socket to a privileged local service is the residual the masks above and P4
  address. **Run genuinely adversarial third-party code only under the P4 dedicated account**,
  not the launching account: the whole-root read bind means the deny-list cannot be proven
  complete, and the account's own files and sockets are the trust boundary until then.

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
