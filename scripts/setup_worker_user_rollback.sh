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

# 1. sudoers first, so no rule can name a removed account even briefly; also clear any leftover temp.
[[ -f "$SUDOERS" ]] && rm -f "$SUDOERS" && echo "removed $SUDOERS"
rm -f "$SUDOERS".tmp.* 2>/dev/null && echo "removed leftover sudoers temp files" || true

# 2. R2: stop any process still running AS the worker, selected by its uid only (never by name match,
#    which could kill an unrelated owner process), before the account is deleted.
if id "$WORKER" >/dev/null 2>&1; then
  WUID=$(dscl . -read "/Users/$WORKER" UniqueID 2>/dev/null | awk '{print $2}')
  if [[ -n "${WUID:-}" ]]; then
    pids=$(ps -U "$WUID" -o pid= 2>/dev/null | tr -s ' ' | tr '\n' ' ')
    if [[ -n "${pids// /}" ]]; then
      echo "stopping worker processes (uid $WUID): $pids"
      kill -TERM ${=pids} 2>/dev/null || true
      sleep 2
      kill -KILL $(ps -U "$WUID" -o pid= 2>/dev/null) 2>/dev/null || true
    fi
  fi
fi

# 3. R1: hand worker-runs back to the owner's own group and drop group/other access, so a later
#    account that happens to reuse the deleted RUNGROUP gid inherits nothing from this directory.
if [[ -d "$RUNS" && ! -L "$RUNS" ]]; then
  [[ -z "$OWNER" ]] && OWNER=$(stat -f %Su "$RUNS" 2>/dev/null || true)
  if [[ -n "$OWNER" ]] && id "$OWNER" >/dev/null 2>&1; then
    chown "$OWNER" "$RUNS" 2>/dev/null || true
    chgrp "$(id -gn "$OWNER")" "$RUNS" 2>/dev/null || true
  fi
  chmod 0700 "$RUNS" 2>/dev/null || true
  echo "neutralised $RUNS (owner-only, 0700)"
fi

# 4. the worker account
if id "$WORKER" >/dev/null 2>&1; then
  dscl . -delete "/Users/$WORKER"
  echo "removed user $WORKER"
fi

# 5. the two groups
for g in "$RUNGROUP" "$WGROUP"; do
  if dscl . -read "/Groups/$g" >/dev/null 2>&1; then
    dscl . -delete "/Groups/$g"
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
