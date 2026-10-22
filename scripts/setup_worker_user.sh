#!/bin/zsh
# One-time setup: a dedicated, unprivileged macOS account that runs the Cairnwork WORKER (agy),
# so the untrusted model worker cannot read Cairnwork's repo, logs, other attempts, or the owner's
# home and the agy account pool (Issue #19 Phase B). The dispatcher (nexus-agy-dispatch, run as the
# owner) launches agy through the whitelisted wrapper /Users/Shared/cairnwork-worker/bin/agy-worker-exec
# with `sudo -n -u _cairnwork_worker`; the wrapper (installed separately by the dispatcher change,
# infra:agy-pool) is the ONLY command this account may run.
#
# Layout (agreed with infra:agy-pool / 1d, option b): the worker tree is its OWN directory,
# /Users/Shared/cairnwork-worker, so it never shares a directory with the verifier's
# /Users/Shared/cairnwork and no other-traverse bit is added to the verifier root. The agy account
# pool (~/.nexus/agy-account-pool) stays private to the owner; the worker only ever sees a per-run
# HOME the dispatcher builds. Concurrent runs are allowed: the dispatcher (as the owner) creates each
# per-run directory under worker-runs with a 128-bit random name at 2770, and the per-run Seatbelt
# profile denies the worker any read/write under worker-runs outside its own run — file permissions
# alone cannot separate runs that share one uid, so the dispatcher half carries that isolation.
#
# Run once, as the owner, with: sudo zsh scripts/setup_worker_user.sh <owner-username>
# Idempotent: running it again re-asserts every invariant and changes nothing already correct.
# Undo with setup_worker_user_rollback.sh.
#
# POST-RUN CHECKLIST for the maintainer (James) — confirm the account is boxed in:
#   id _cairnwork_worker                                   # must show NO staff and NO admin
#   dscl . -read /Users/_cairnwork_worker UserShell IsHidden
#   sudo -l -U <owner>                                     # must show ONLY the one agy-worker-exec rule
#   dscl . -read /Groups/_cairnwork_wrun GroupMembership   # must list exactly two members (owner, worker)
#   sudo -u _cairnwork_worker /bin/ls /Users/<owner>       # must say Permission denied
# The wrapper agy-worker-exec must be installed by the dispatcher change before any end-to-end test.
# To roll back: sudo zsh scripts/setup_worker_user_rollback.sh
set -euo pipefail

OWNER="${1:?usage: sudo zsh setup_worker_user.sh <owner-username>}"
WORKER="_cairnwork_worker"            # the worker account; its primary group is WGROUP (never staff)
WGROUP="_cairnwork_worker"            # the worker's own primary group, so it is not in staff (gid 20)
RUNGROUP="_cairnwork_wrun"            # shared run group: owner + worker, setgid on the run dirs
ROOT="/Users/Shared/cairnwork-worker" # the worker's OWN tree, separate from the verifier's
RUNS="$ROOT/worker-runs"              # the dispatcher creates each per-run dir here (2770, random name)
BIN="$ROOT/bin"                       # the dispatcher installs agy-worker-exec here (owner, 0755)
WRAPPER="$BIN/agy-worker-exec"
SUDOERS="/etc/sudoers.d/cairnwork-worker"

[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }

# S5. the owner must be a plausible local account name and never root.
[[ "$OWNER" =~ '^[a-z_][a-z0-9_-]*$' ]] || { echo "owner '$OWNER' is not a valid account name" >&2; exit 1; }
[[ "$OWNER" != "root" ]] || { echo "owner must not be root" >&2; exit 1; }
id "$OWNER" >/dev/null

# S1. refuse to operate on a path an unprivileged agent could have planted as a symlink: a
#     'worker-runs -> /Users/<owner>' link would otherwise make us chmod the owner's home. Each level
#     must be either absent or already a real directory, never a symlink.
require_real_dir_or_absent() {
  if [[ -L "$1" ]]; then echo "refusing: $1 is a symlink" >&2; exit 1; fi
  if [[ -e "$1" && ! -d "$1" ]]; then echo "refusing: $1 exists but is not a directory" >&2; exit 1; fi
}
for p in "$ROOT" "$BIN" "$RUNS"; do require_real_dir_or_absent "$p"; done

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
fi
# S2. re-assert the account's locked-down attributes on EVERY run, not only at creation, so a later
#     edit that loosened them is corrected here.
dscl . -create "/Users/$WORKER" UserShell /usr/bin/false
dscl . -create "/Users/$WORKER" NFSHomeDirectory /var/empty
dscl . -create "/Users/$WORKER" RealName "Cairnwork worker"
dscl . -create "/Users/$WORKER" IsHidden 1
dscl . -create "/Users/$WORKER" Password '*'
dscl . -create "/Users/$WORKER" PrimaryGroupID "$WGID"
WUID=$(dscl . -read "/Users/$WORKER" UniqueID | awk '{print $2}')
(( WUID < 500 )) || { echo "worker uid $WUID is not a service id (<500); refusing" >&2; exit 1; }
# the worker must not be in staff or admin; confirm its primary group and group memberships
wprimary=$(dscl . -read "/Users/$WORKER" PrimaryGroupID | awk '{print $2}')
[[ "$wprimary" == "$WGID" ]] || { echo "worker primary group is $wprimary, not $WGROUP ($WGID)" >&2; exit 1; }
for forbidden in staff admin; do
  id -Gn "$WORKER" | tr ' ' '\n' | grep -qx "$forbidden" \
    && { echo "worker is in $forbidden; refusing" >&2; exit 1; }
  dsmemberutil checkmembership -U "$WORKER" -G "$forbidden" 2>/dev/null | grep -q "is a member" \
    && { echo "worker is a member of $forbidden; refusing" >&2; exit 1; }
done

# 3. shared run group holds exactly the owner and the worker (owner reads results back by group).
dseditgroup -o edit -a "$OWNER" -t user "$RUNGROUP"
dseditgroup -o edit -a "$WORKER" -t user "$RUNGROUP"
# S3. read the membership back: it must be exactly {owner, worker}, and the group must nest no other
#     group (a nested group could smuggle in extra members).
members=$(dscl . -read "/Groups/$RUNGROUP" GroupMembership 2>/dev/null \
  | sed 's/^GroupMembership://' | tr ' ' '\n' | grep -v '^$' | sort | tr '\n' ' ')
expected=$(printf '%s\n' "$OWNER" "$WORKER" | sort | tr '\n' ' ')
[[ "$members" == "$expected" ]] \
  || { echo "run group members are {$members}, expected {$expected}; refusing" >&2; exit 1; }
nested=$(dscl . -read "/Groups/$RUNGROUP" NestedGroups 2>/dev/null \
  | sed 's/^NestedGroups://' | tr -d ' ' || true)   # the key is normally absent -> non-zero, that is fine
[[ -z "$nested" ]] || { echo "run group has nested groups ($nested); refusing" >&2; exit 1; }
# creating the account may have added it to the file-sharing group; remove it (it grants share access).
if dseditgroup -o checkmember -m "$WORKER" com.apple.sharepoint.group.1 >/dev/null 2>&1; then
  dseditgroup -o edit -d "$WORKER" -t user com.apple.sharepoint.group.1 2>/dev/null || true
  echo "note: removed $WORKER from com.apple.sharepoint.group.1"
fi

# 4. directories, all under the worker's own root (nothing touches the verifier's /Users/Shared/cairnwork).
mkdir -p "$RUNS" "$BIN"
for p in "$ROOT" "$BIN" "$RUNS"; do
  [[ -d "$p" && ! -L "$p" ]] || { echo "refusing: $p is not a real directory after mkdir" >&2; exit 1; }
done
#    ROOT: root-owned, world-readable+traversable, no world write (root:wheel 0755).
chown root:wheel "$ROOT"
chmod 0755 "$ROOT"
#    bin: owner-owned 0755 (the dispatcher installs the wrapper there; the worker cannot modify it).
chown "$OWNER" "$BIN"
chmod 0755 "$BIN"
#    worker-runs: owner-owned, group RUNGROUP, setgid + 2710 — the owner (dispatcher) creates/renames
#    per-run dirs; the worker's group may only TRAVERSE into a run dir it is handed, not write the
#    parent, so it cannot list, rename or replace a sibling run's directory.
chown "$OWNER" "$RUNS"
chgrp "$RUNGROUP" "$RUNS"
chmod 2710 "$RUNS"

# 5. the owner may run, as the worker account, ONLY the wrapper (never ALL). The wrapper validates
#    that every path it is given is inside worker-runs, then execs sandbox-exec on agy.
#    S4. write the drop-in with a tight umask, root:wheel ownership and 0440 BEFORE it is installed,
#    so it is never briefly writable or owned by anyone but root, and validate it before mv.
mkdir -p /etc/sudoers.d
TMP_SUDOERS="$SUDOERS.tmp.$$"
umask 077
cat > "$TMP_SUDOERS" <<EOF
$OWNER ALL=($WORKER) NOPASSWD: $WRAPPER
EOF
chown root:wheel "$TMP_SUDOERS"
chmod 0440 "$TMP_SUDOERS"
if ! visudo -cf "$TMP_SUDOERS" >/dev/null; then
  rm -f "$TMP_SUDOERS"               # invalid drop-in: install nothing and leave no leftover
  echo "sudoers drop-in failed validation; nothing installed" >&2
  exit 1
fi
mv "$TMP_SUDOERS" "$SUDOERS"

echo "worker account: $(id "$WORKER")"
echo "run group:      $(dscl . -read /Groups/$RUNGROUP GroupMembership 2>/dev/null)"
echo "root:           $(ls -ld "$ROOT")"
echo "worker-runs:    $(ls -ld "$RUNS")"
echo "bin:            $(ls -ld "$BIN")  (dispatcher installs $WRAPPER)"
echo "sudo rule:      $(cat "$SUDOERS")"
echo "note: the wrapper $WRAPPER is installed by the dispatcher change (infra:agy-pool), not here."
