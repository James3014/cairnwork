#!/bin/zsh
# One-time setup: a dedicated, unprivileged macOS account that runs the Cairnwork WORKER (agy),
# so the untrusted model worker cannot read Cairnwork's repo, logs, other attempts, or the owner's
# home and the agy account pool (Issue #19 Phase B). The dispatcher (nexus-agy-dispatch, run as the
# owner) launches agy through the whitelisted wrapper /Users/Shared/cairnwork-worker/bin/agy-worker-exec
# with `sudo -n -u _cairnwork_worker`; the wrapper (installed separately by the dispatcher change,
# infra:agy-pool) is the only command the sudoers rule lets the OWNER start as this account. (The rule
# says nothing about what code running as the worker may do; that boundary is the account's lack of
# group/file access plus the dispatcher's per-run Seatbelt profile.)
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
# Trust model notes:
#   * BIN is owner-owned (e.g. james) 0755: the worker can read and execute the wrapper but cannot write
#     BIN or replace the wrapper. Anything running AS THE OWNER can replace it; that is in-model, because
#     the owner already controls the dispatcher and the sudoers rule only lets the owner start the worker.
#   * Every run uses the SAME worker uid, so cross-run isolation (one run reading or writing another run's
#     directory) relies on the dispatcher's per-run Seatbelt profile, not on file permissions.
#   * Modes are checked with stat '%Mp%Lp' (4 octal digits): '%Lp' alone drops setuid/setgid/sticky, so
#     a 2710 directory would print 710.
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
# Until it exists the sudoers rule is INERT and the summary prints "wrapper ... PENDING"; the wrapper-level
# witnesses (agy via the wrapper, cross-run Seatbelt denial) are then pending, not passed.
# Prerequisite: /Users/<owner> must not be readable or traversable by others (o-rx); setup refuses otherwise.
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

die() { echo "refusing: $*" >&2; exit 1; }
TMP_SUDOERS="$SUDOERS.tmp.$$"
# Fail closed on re-runs: if ANY invariant below fails, the previously installed sudoers rule is removed
# (and the temp file), so a worker that now violates an invariant cannot still be launched.
TESTRUN=""   # the witness's temporary run dir (created and removed AS THE OWNER, never by root)
trap 'rc=$?; [[ -n "$TESTRUN" ]] && sudo -n -u "$OWNER" /bin/rm -rf "$TESTRUN" 2>/dev/null; if (( rc != 0 )); then rm -f "$SUDOERS" "$TMP_SUDOERS"; echo "setup failed: sudoers rule removed (if any); nothing is enabled" >&2; fi' EXIT

# S1. refuse to operate on a path an unprivileged agent could have planted. /Users/Shared is
#     world-writable (sticky), so ROOT is adopted only if it is absent (we then create it as root) or
#     already a real directory OWNED BY ROOT: only root can then replace anything beneath it, which
#     closes the check-to-use window for BIN and RUNS. Children must be absent or real directories
#     owned by root or the owner, never symlinks.
require_real_dir_or_absent() {   # $1 path, $2.. allowed owners
  local p="$1"; shift
  [[ ! -L "$p" ]] || die "$p is a symlink"
  [[ ! -e "$p" || -d "$p" ]] || die "$p exists but is not a directory"
  if [[ -e "$p" ]]; then
    local o; o=$(stat -f %Su "$p") || die "cannot stat $p"
    (( ${@[(Ie)$o]} )) || die "$p is owned by '$o' (expected: $*); inspect and remove it by hand"
  fi
}
[[ ! -L /Users/Shared && -d /Users/Shared ]] || die "/Users/Shared is not a real directory"
# Prerequisite: the owner's home must give OTHERS no read or traverse bit. The worker's only route into it
# would then be a group it is in, and the group checks below refuse those. (Default macOS homes are 0750.)
ohome="/Users/$OWNER"
[[ -d "$ohome" ]] || die "owner home $ohome does not exist"
omode=$(stat -f %Lp "$ohome") || die "cannot stat $ohome"
(( (8#$omode & 8#005) == 0 )) || die "$ohome is mode $omode (others can read or traverse it); run 'chmod o-rx $ohome' and re-run"
require_real_dir_or_absent "$ROOT" root
# First run: create ROOT RIGHT HERE with plain mkdir (no -p). It fails if anything (a planted symlink or
# directory) appeared since the check, and a root-created directory is root-owned from the first instant,
# so no later chown/chmod can be redirected through a link.
[[ -e "$ROOT" ]] || mkdir "$ROOT" || die "cannot create $ROOT (something appeared there?)"
[[ ! -L "$ROOT" && -d "$ROOT" && "$(stat -f %Su "$ROOT")" == root ]] || die "$ROOT is not a root-owned real directory"
require_real_dir_or_absent "$BIN" root "$OWNER"
require_real_dir_or_absent "$RUNS" root "$OWNER"

# a hidden service id in the quiet range 450..499 that is used by NO user (UniqueID) and NO group
# (PrimaryGroupID), so the uid and gid spaces never overlap with an existing account or group. (max+1
# would land next to Apple's own service ids, which new macOS releases keep adding.)
free_id() {
  local used id
  used=$( { dscl . -list /Users UniqueID; dscl . -list /Groups PrimaryGroupID; } | awk '{print $2}') \
    || { echo "cannot list existing uids/gids" >&2; exit 1; }
  for (( id = 450; id < 500; id++ )); do
    print -r -- "$used" | grep -qx "$id" || { echo "$id"; return 0; }
  done
  echo "no id in 450..499 is free for both users and groups" >&2; exit 1
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
# the worker's uid and gid must belong to no other account/group, or the kernel would treat the worker
# as that account (e.g. the verifier) for file ownership.
uid_n=$(dscl . -list /Users UniqueID | awk -v u="$WUID" '$2==u' | wc -l | tr -d ' ')
[[ "$uid_n" == "1" ]] || die "worker uid $WUID is shared by $uid_n accounts"
gid_n=$(dscl . -list /Groups PrimaryGroupID | awk -v g="$WGID" '$2==g' | wc -l | tr -d ' ')
[[ "$gid_n" == "1" ]] || die "worker gid $WGID is shared by $gid_n groups"
# the worker must not be in staff or admin; confirm its primary group and group memberships.
# Flush both directory caches first so id(1) and dsmemberutil see the records just written.
flush_caches() { dscacheutil -flushcache 2>/dev/null || true; dsmemberutil flushcache 2>/dev/null || true; }
flush_caches
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
# the NestedGroups key is normally absent (dscl exits non-zero with "No such key"); any other failure
# or any value is refused.
if nested=$(dscl . -read "/Groups/$RUNGROUP" NestedGroups 2>&1); then
  die "run group has nested groups ($nested)"
else
  [[ "$nested" == *"No such key"* ]] || die "cannot read NestedGroups of $RUNGROUP: $nested"
fi
# creating the account may have added it to the file-sharing group; remove it and READ IT BACK.
for g in com.apple.sharepoint.group.1; do
  if dscl . -search /Groups GroupMembership "$WORKER" | awk '/GroupMembership/{print $1}' | grep -qx "$g"; then
    dseditgroup -o edit -d "$WORKER" -t user "$g" || die "could not remove $WORKER from $g"
    echo "note: removed $WORKER from $g"
  fi
done
flush_caches
# G. the worker's complete group set must be exactly {WGROUP, RUNGROUP}: any other group (cairnwork,
#    which owns the verifier's logs, sharepoint, a custom group) could grant read access to protected
#    data. Check both the directory's direct memberships and the effective set from id(1).
direct=$(dscl . -search /Groups GroupMembership "$WORKER" | awk '/GroupMembership/{print $1}' | sort | tr '\n' ' ')
[[ "$direct" == "$RUNGROUP " ]] || die "worker is a direct member of {$direct}, expected only {$RUNGROUP}"
# Implicit groups the OS adds to every account (everyone, localaccounts, com.apple.sharepoint.group.1,
# _lpoperator ...) are expected and harmless, so the effective set is not compared for equality.
# Instead refuse any group that grants access to something protected: staff, admin, wheel, cairnwork
# (verifier logs), the owner's primary group, and any group that owns the owner's home, the verifier root
# or the verifier runs directory.
forbidden=(staff admin wheel cairnwork "$(id -gn "$OWNER")")
for pth in "/Users/$OWNER" /Users/Shared/cairnwork /Users/Shared/cairnwork/runs; do
  [[ -e "$pth" ]] && forbidden+=("$(stat -f %Sg "$pth")")
done
effective=(${(f)"$(id -Gn "$WORKER" | tr ' ' '\n')"})
[[ ${#effective} -gt 0 ]] || die "cannot read the effective groups of $WORKER"
(( ${effective[(Ie)$WGROUP]} && ${effective[(Ie)$RUNGROUP]} )) || die "worker effective groups {$effective} lack $WGROUP/$RUNGROUP"
for g in $effective; do
  if (( ${forbidden[(Ie)$g]} )); then die "worker is in protected group '$g' (effective groups: $effective)"; fi
done
# GroupMembers (GUIDs) is honoured by the kernel as well as GroupMembership (names): every GUID listed
# must belong to the owner or the worker.
okguids=("$(dscl . -read "/Users/$OWNER" GeneratedUID | awk '{print $2}')" "$(dscl . -read "/Users/$WORKER" GeneratedUID | awk '{print $2}')")
if gm=$(dscl . -read "/Groups/$RUNGROUP" GroupMembers 2>&1); then
  for guid in ${=${gm#GroupMembers:}}; do
    (( ${okguids[(Ie)$guid]} )) || die "run group GroupMembers lists an unexpected GUID $guid"
  done
else
  [[ "$gm" == *"No such key"* ]] || die "cannot read GroupMembers of $RUNGROUP: $gm"
fi

# 4. directories, all under the worker's own root (nothing touches the verifier's /Users/Shared/cairnwork).
umask 022
[[ ! -L "$ROOT" && -d "$ROOT" && "$(stat -f %Su "$ROOT")" == root ]] || die "$ROOT is not a root-owned real directory"
chown -h root:wheel "$ROOT"; chmod 0755 "$ROOT"; chmod -N "$ROOT"
for p in "$BIN" "$RUNS"; do [[ -d "$p" ]] || mkdir "$p"; done
for p in "$BIN" "$RUNS"; do [[ -d "$p" && ! -L "$p" ]] || die "$p is not a real directory after mkdir"; done
#    bin: owner-owned 0755 (the dispatcher installs the wrapper there; the worker cannot modify it).
chown "$OWNER" "$BIN"; chmod 0755 "$BIN"
#    worker-runs: owner-owned, group RUNGROUP, setgid + 2710 — the owner (dispatcher) creates/renames
#    per-run dirs; the worker's group may only TRAVERSE into a run dir it is handed, not write the
#    parent, so it cannot list, rename or replace a sibling run's directory.
chown "$OWNER" "$RUNS"; chgrp "$RUNGROUP" "$RUNS"; chmod 2710 "$RUNS"
#    ACLs survive chmod and can grant the worker write access: strip them, then verify every directory
#    (owner, group, mode, no ACL, no symlink) before the sudoers rule exists.
for p in "$BIN" "$RUNS"; do chmod -N "$p"; done
verify_dir() {   # $1 path  $2 owner  $3 group  $4 mode as 4 octal digits (e.g. 0755, 2710)
  local st; st=$(stat -f '%Su %Sg %Mp%Lp' "$1") || die "cannot stat $1"
  [[ "$st" == "$2 $3 $4" ]] || die "$1 is '$st', expected '$2 $3 $4'"
  [[ ! -L "$1" ]] || die "$1 is a symlink"
  [[ -z "$(ls -lde "$1" | sed -n '2,$p')" && "$(ls -ld "$1" | cut -c11)" != "+" ]] || die "$1 still has an ACL"
}
verify_dir "$ROOT" root wheel 0755
verify_dir "$BIN" "$OWNER" "$(stat -f %Sg "$BIN")" 0755
[[ "$(stat -f %Sg "$BIN")" != "$RUNGROUP" && "$(stat -f %Sg "$BIN")" != "$WGROUP" ]] || die "$BIN is group-owned by a worker group"
verify_dir "$RUNS" "$OWNER" "$RUNGROUP" 2710

# 5. the owner may run, as the worker account, ONLY the wrapper (never ALL). The wrapper validates
#    that every path it is given is inside worker-runs, then execs sandbox-exec on agy.
#    S4. write the drop-in with a tight umask, root:wheel ownership and 0440 BEFORE it is installed,
#    so it is never briefly writable or owned by anyone but root, and validate it before mv.
mkdir -p /etc/sudoers.d
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

# H. isolation witness, BEFORE the rule goes live: actually run as the worker and require that it is
#    denied the owner's home, the verifier's tree, the worker-runs listing, and any write into bin.
#    A path that is readable (or writable) makes setup fail non-zero.
#    Positive controls first: a denial only means something if sudo really runs commands as the worker,
#    so the worker must report its own uid, and it must be able to traverse into a run directory it is
#    handed (the same 2770 shape the dispatcher creates) and read a file there.
wid=$(sudo -n -u "$WORKER" /usr/bin/id -u 2>/dev/null) || die "ISOLATION WITNESS FAILED: cannot run a command as $WORKER (positive control)"
[[ "$wid" == "$WUID" ]] || die "ISOLATION WITNESS FAILED: command run as $WORKER reports uid '$wid', expected $WUID"
echo "witness ok: commands run as $WORKER (uid $WUID)"
#    The test run dir is made AS THE OWNER, exactly as the dispatcher does: worker-runs is owner-writable,
#    so root must not create, chown or write anything inside it (a planted symlink would redirect root).
rnd=$(openssl rand -hex 16) || die "cannot generate a random run name"
TESTRUN="$RUNS/witness-$rnd"
sudo -n -u "$OWNER" /bin/mkdir -m 2770 "$TESTRUN" || die "cannot create test run dir $TESTRUN as $OWNER"
sudo -n -u "$OWNER" /bin/sh -c 'umask 027; printf witness > "$1/probe"' sh "$TESTRUN" \
  || die "cannot write the probe file as $OWNER"
got=$(sudo -n -u "$WORKER" /bin/cat "$TESTRUN/probe" 2>/dev/null) || got=""
[[ "$got" == witness ]] || die "ISOLATION WITNESS FAILED: worker cannot read a file in a run dir it is handed ($TESTRUN)"
echo "witness ok: worker can traverse a handed run dir"
witness_deny() {   # $1 description; rest = command run as the worker; it must FAIL
  local what="$1"; shift
  if sudo -n -u "$WORKER" "$@" >/dev/null 2>&1; then die "ISOLATION WITNESS FAILED: worker can $what"; fi
  echo "witness ok: worker denied: $what"
}
witness_deny "list the owner's home /Users/$OWNER" /bin/ls "/Users/$OWNER"
witness_deny "list worker-runs" /bin/ls "$RUNS"
witness_deny "write into $BIN" /usr/bin/touch "$BIN/.witness.$$"
rm -f "$BIN/.witness.$$"
for v in /Users/Shared/cairnwork /Users/Shared/cairnwork/runs; do
  [[ -e "$v" ]] && witness_deny "list the verifier tree $v" /bin/ls "$v"
done
sudo -n -u "$OWNER" /bin/rm -rf "$TESTRUN" || die "cannot remove test run dir $TESTRUN"
TESTRUN=""
# The wrapper is NOT installed by this script (infra:agy-pool / 1d's dispatcher change installs it). If it is
# present it must be a real, owner-owned 0755 file; if it is absent the rule is inert and the wrapper-level
# witnesses (agy through the wrapper, cross-run Seatbelt denial) are PENDING, not passed.
if [[ -e "$WRAPPER" || -L "$WRAPPER" ]]; then
  [[ ! -L "$WRAPPER" && -f "$WRAPPER" ]] || die "$WRAPPER is a symlink or not a regular file"
  wst=$(stat -f '%Su %Mp%Lp' "$WRAPPER") || die "cannot stat $WRAPPER"
  [[ "$wst" == "$OWNER 0755" ]] || die "$WRAPPER is '$wst', expected '$OWNER 0755'"
  WRAPPER_STATE="installed ($wst); run the wrapper-level witnesses in the runbook now"
else
  WRAPPER_STATE="NOT INSTALLED - the sudo rule is INERT until the dispatcher change installs it; wrapper-level witnesses PENDING"
fi
mv "$TMP_SUDOERS" "$SUDOERS"

echo "worker account: $(id "$WORKER")"
echo "run group:      $(dscl . -read /Groups/$RUNGROUP GroupMembership 2>/dev/null)"
echo "root:           $(ls -ld "$ROOT")"
echo "worker-runs:    $(ls -ld "$RUNS")"
echo "bin:            $(ls -ld "$BIN")  (dispatcher installs $WRAPPER)"
echo "sudo rule:      $(cat "$SUDOERS")"
echo "wrapper:        $WRAPPER: $WRAPPER_STATE"
echo "note: the wrapper is installed by the dispatcher change (infra:agy-pool), not here."
