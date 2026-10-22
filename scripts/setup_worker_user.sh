#!/bin/zsh
# One-time setup: a dedicated, unprivileged macOS account that runs the Cairnwork WORKER (agy),
# so the untrusted model worker cannot read Cairnwork's repo, logs, other attempts, or the owner's
# home and the agy account pool (Issue #19 Phase B). The dispatcher (nexus-agy-dispatch, run as the
# owner) launches agy through the whitelisted wrapper /Users/Shared/cairnwork/bin/agy-worker-exec
# with `sudo -n -u _cairnwork_worker`; the wrapper (installed separately by the dispatcher change)
# is the ONLY command this account may run.
#
# Design agreed with infra:agy-pool (1d), Revision 1: the pool (~/.nexus/agy-account-pool) stays
# private to the owner; the worker only ever sees a per-run HOME the dispatcher builds. Run roots
# live under /Users/Shared/cairnwork/worker-runs, a dedicated group _cairnwork_wrun (owner +
# worker), setgid, 0770; the wrapper sets umask 007 so the owner reads results back by group and
# deletes them. No chown is used (a non-root owner cannot chown on macOS).
#
# Run once, as the owner, with: sudo zsh scripts/setup_worker_user.sh <owner-username>
# Idempotent: running it again changes nothing already in place. Undo with setup_worker_user_rollback.sh.
set -euo pipefail

OWNER="${1:?usage: sudo zsh setup_worker_user.sh <owner-username>}"
WORKER="_cairnwork_worker"      # the worker account; its primary group is WGROUP (never staff)
WGROUP="_cairnwork_worker"      # the worker's own primary group, so it is not in staff (gid 20)
RUNGROUP="_cairnwork_wrun"      # shared run group: owner + worker, setgid on the run dirs
ROOT="/Users/Shared/cairnwork"
RUNS="$ROOT/worker-runs"        # separate from the verifier's $ROOT/runs
BIN="$ROOT/bin"                 # the dispatcher installs agy-worker-exec here (owner, 0755)
WRAPPER="$BIN/agy-worker-exec"
SUDOERS="/etc/sudoers.d/cairnwork-worker"

[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
id "$OWNER" >/dev/null

# a hidden service id below 500 that no user or group uses yet
free_id() {
  local id=$(( $(dscl . -list "$1" "$2" | awk '{print $2}' | sort -n | awk '$1<500' | tail -1) + 1 ))
  (( id < 500 )) || { echo "no free id below 500 in $1" >&2; exit 1; }
  dscl . -list "$1" "$2" | awk '{print $2}' | grep -qx "$id" && { echo "id $id in use" >&2; exit 1; }
  echo "$id"
}

# 1. the worker's own primary group (so its PrimaryGroupID is never staff), and the shared run group
for g in "$WGROUP" "$RUNGROUP"; do
  if ! dscl . -read "/Groups/$g" >/dev/null 2>&1; then
    gid=$(free_id /Groups PrimaryGroupID)
    dscl . -create "/Groups/$g"
    dscl . -create "/Groups/$g" PrimaryGroupID "$gid"
  fi
done
WGID=$(dscl . -read "/Groups/$WGROUP" PrimaryGroupID | awk '{print $2}')

# 2. hidden worker account: no shell, no home, no password, primary group WGROUP (not staff)
if ! id "$WORKER" >/dev/null 2>&1; then
  uid=$(free_id /Users UniqueID)
  dscl . -create "/Users/$WORKER"
  dscl . -create "/Users/$WORKER" UniqueID "$uid"
  dscl . -create "/Users/$WORKER" PrimaryGroupID "$WGID"
  dscl . -create "/Users/$WORKER" UserShell /usr/bin/false
  dscl . -create "/Users/$WORKER" NFSHomeDirectory /var/empty
  dscl . -create "/Users/$WORKER" RealName "Cairnwork worker"
  dscl . -create "/Users/$WORKER" IsHidden 1
  dscl . -create "/Users/$WORKER" Password "*"
fi
# the worker must not be in staff; confirm its primary group is WGROUP
wprimary=$(dscl . -read "/Users/$WORKER" PrimaryGroupID | awk '{print $2}')
[[ "$wprimary" == "$WGID" ]] || { echo "worker primary group is $wprimary, not $WGROUP ($WGID)" >&2; exit 1; }
id -Gn "$WORKER" | tr ' ' '\n' | grep -qx staff && { echo "worker is in staff; refusing" >&2; exit 1; }

# 3. shared run group holds exactly the owner and the worker (owner reads results back by group)
dseditgroup -o edit -a "$OWNER" -t user "$RUNGROUP"
dseditgroup -o edit -a "$WORKER" -t user "$RUNGROUP"

# 4. directories.
#    $ROOT may already be the verifier's 2750 owner:cairnwork (no other-traverse); the worker is in
#    neither that owner nor that group, so add other-execute (traverse only, no listing) so it can
#    descend to worker-runs/bin. The verifier's own runs subdir stays 2750 group cairnwork, which
#    the worker (not in cairnwork) still cannot enter. (chmod o+x is additive: owner/group/setgid
#    and the absence of other-read are preserved.)
mkdir -p "$RUNS" "$BIN"
chmod o+x "$ROOT"
#    worker-runs: setgid so the per-run dirs the DISPATCHER (as the owner) creates inherit RUNGROUP.
#    Mode 2710 — owner (dispatcher) creates/renames per-run dirs; the worker's group may only
#    traverse into its own per-run dir, NOT write the parent, so it cannot rename or replace a
#    sibling attempt's directory. Each per-run dir is opened to the group (2770) by the dispatcher.
chgrp "$RUNGROUP" "$RUNS"
chmod 2710 "$RUNS"
#    bin: owner-owned 0755 (the dispatcher installs the wrapper there; the worker cannot modify it).
chown "$OWNER" "$BIN"
chmod 0755 "$BIN"

# 5. the owner may run, as the worker account, ONLY the wrapper (never ALL). The wrapper validates
#    that every path it is given is inside worker-runs, then execs sandbox-exec on agy.
mkdir -p /etc/sudoers.d
cat > "$SUDOERS.tmp" <<EOF
$OWNER ALL=($WORKER) NOPASSWD: $WRAPPER
EOF
chmod 0440 "$SUDOERS.tmp"
visudo -cf "$SUDOERS.tmp" >/dev/null   # if the drop-in is invalid, nothing is installed
mv "$SUDOERS.tmp" "$SUDOERS"

echo "worker account: $(id "$WORKER")"
echo "run group:      $(dscl . -read /Groups/$RUNGROUP GroupMembership 2>/dev/null)"
echo "worker-runs:    $(ls -ld "$RUNS")"
echo "bin:            $(ls -ld "$BIN")  (dispatcher installs $WRAPPER)"
echo "sudo rule:      $(cat "$SUDOERS")"
echo "note: the wrapper $WRAPPER is installed by the dispatcher change (infra:agy-pool), not here."
