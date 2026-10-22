#!/bin/zsh
# Undo scripts/setup_worker_user.sh (Issue #19 Phase B): stop any running worker process, remove the
# sudoers drop-in (and any leftover temp file), the worker account and its two groups, and neutralise
# the run directory so a group id reused later cannot inherit access. The run/bin directories under
# /Users/Shared/cairnwork-worker are left in place by default (they hold no secrets); pass
# --purge-dirs to delete the empty worker-runs, bin and root directories too.
#
# Run as the owner with: sudo zsh scripts/setup_worker_user_rollback.sh [<owner-username>] [--purge-dirs]
# The owner is only needed to hand worker-runs back to the owner's group; it is read from the
# directory itself when omitted. Idempotent: anything already absent is skipped.
set -euo pipefail

WORKER="_cairnwork_worker"
WGROUP="_cairnwork_worker"
RUNGROUP="_cairnwork_wrun"
ROOT="/Users/Shared/cairnwork-worker"
RUNS="$ROOT/worker-runs"
BIN="$ROOT/bin"
SUDOERS="/etc/sudoers.d/cairnwork-worker"

[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }

OWNER=""
PURGE=""
for arg in "$@"; do
  case "$arg" in
    --purge-dirs) PURGE="--purge-dirs" ;;
    *) OWNER="$arg" ;;
  esac
done

die() { echo "ROLLBACK FAILED: $*" >&2; echo "rollback stopped before any later step; fix the cause and run it again." >&2; exit 1; }

# Path guard: ROOT's ancestors and ROOT itself must be real directories, and ROOT must be root-owned
# (setup creates it that way). A symlinked or attacker-owned ROOT could redirect chown/chmod below.
[[ ! -L /Users/Shared && -d /Users/Shared ]] || die "/Users/Shared is not a real directory"
if [[ -e "$ROOT" || -L "$ROOT" ]]; then
  [[ ! -L "$ROOT" && -d "$ROOT" ]] || die "$ROOT is a symlink or not a directory"
  [[ "$(stat -f %Su "$ROOT")" == root ]] || die "$ROOT is not root-owned; inspect it by hand"
fi

# 1. sudoers first, so no rule can name a removed account even briefly; also clear any leftover temp.
#    (the (N) qualifier makes an unmatched glob expand to nothing instead of aborting zsh.)
for f in "$SUDOERS" "$SUDOERS".tmp.*(N); do
  if [[ -e "$f" || -L "$f" ]]; then rm -f "$f" || die "cannot remove $f"; echo "removed $f"; fi
done
for f in "$SUDOERS" "$SUDOERS".tmp.*(N); do [[ ! -e "$f" && ! -L "$f" ]] || die "$f still exists"; done

# 2. R2: stop any process still running AS the worker, selected by its uid only (never by name match,
#    which could kill an unrelated owner process), and verify none is left before the account goes.
worker_pids() {   # prints pids; pgrep rc 1 = none, anything else is an error
  local out rc=0
  out=$(pgrep -U "$1") || rc=$?
  (( rc <= 1 )) || die "pgrep failed (rc $rc)"
  print -r -- "$out"
}
if dscl . -read "/Users/$WORKER" UniqueID >/dev/null 2>&1; then
  WUID=$(dscl . -read "/Users/$WORKER" UniqueID | awk '{print $2}')
  [[ "$WUID" == <-> ]] || die "cannot read the worker uid"
  (( WUID > 0 )) || die "worker uid is 0"
  pids=$(worker_pids "$WUID")
  if [[ -n "$pids" ]]; then
    echo "stopping worker processes (uid $WUID): ${pids//$'\n'/ }"
    kill -TERM ${(f)pids} 2>/dev/null || true   # a pid may already have exited; verified below
    for i in 1 2 3 4 5; do sleep 1; [[ -z "$(worker_pids "$WUID")" ]] && break; done
    left=$(worker_pids "$WUID")
    if [[ -n "$left" ]]; then
      kill -KILL ${(f)left} 2>/dev/null || true
      sleep 1
    fi
    [[ -z "$(worker_pids "$WUID")" ]] || die "worker processes still running after KILL: $(worker_pids "$WUID" | tr '\n' ' ')"
  fi
fi

# 3. R1: hand worker-runs back to the owner's own group and drop group/other access and ACLs, so a later
#    account that happens to reuse the deleted RUNGROUP gid inherits nothing. Every step must succeed
#    and is read back; a failure stops the rollback BEFORE the group/account is deleted.
if [[ -e "$RUNS" || -L "$RUNS" ]]; then
  [[ ! -L "$RUNS" && -d "$RUNS" ]] || die "$RUNS is a symlink or not a directory"
  [[ -n "$OWNER" ]] || OWNER=$(stat -f %Su "$RUNS") || die "cannot stat $RUNS"
  [[ "$OWNER" != root ]] || die "$RUNS is owned by root; pass the owner username explicitly"
  id "$OWNER" >/dev/null 2>&1 || die "owner '$OWNER' is not an account"
  OGROUP=$(id -gn "$OWNER") || die "cannot read $OWNER's primary group"
  chown "$OWNER" "$RUNS" || die "chown $RUNS failed"
  chgrp "$OGROUP" "$RUNS" || die "chgrp $RUNS failed"
  chmod 0700 "$RUNS" || die "chmod $RUNS failed"
  chmod -N "$RUNS" || die "cannot clear ACLs on $RUNS"
  [[ "$(stat -f '%Su %Sg %Lp' "$RUNS")" == "$OWNER $OGROUP 700" ]] || die "$RUNS is not '$OWNER $OGROUP 700' after neutralising"
  [[ -z "$(ls -lde "$RUNS" | sed -n '2,$p')" ]] || die "$RUNS still has an ACL"
  echo "neutralised $RUNS (owner-only, 0700, no ACL)"
fi

# 4. the worker account
if dscl . -read "/Users/$WORKER" >/dev/null 2>&1; then
  dscl . -delete "/Users/$WORKER" || die "cannot delete user $WORKER"
  dscl . -read "/Users/$WORKER" >/dev/null 2>&1 && die "user $WORKER still exists"
  echo "removed user $WORKER"
fi

# 5. the two groups
for g in "$RUNGROUP" "$WGROUP"; do
  if dscl . -read "/Groups/$g" >/dev/null 2>&1; then
    dscl . -delete "/Groups/$g" || die "cannot delete group $g"
    dscl . -read "/Groups/$g" >/dev/null 2>&1 && die "group $g still exists"
    echo "removed group $g"
  fi
done

# 6. directories only if asked, and only when empty (never delete run data blindly)
if [[ "$PURGE" == "--purge-dirs" ]]; then
  for d in "$RUNS" "$BIN" "$ROOT"; do
    if [[ -d "$d" ]]; then
      rmdir "$d" 2>/dev/null && echo "removed empty $d" || echo "kept non-empty $d (remove by hand)"
    fi
  done
fi

echo "rollback done. the verifier account and /Users/Shared/cairnwork are untouched."
