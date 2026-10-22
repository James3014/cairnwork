#!/bin/zsh
# Undo scripts/setup_worker_user.sh (Issue #19 Phase B): remove the sudoers drop-in, the worker
# account and its two groups. The run/bin directories under /Users/Shared/cairnwork are left in
# place by default (they hold no secrets and are shared service data); pass --purge-dirs to delete
# the empty worker-runs and bin directories too.
#
# Run as the owner with: sudo zsh scripts/setup_worker_user_rollback.sh [--purge-dirs]
# Idempotent: anything already absent is skipped.
set -euo pipefail

WORKER="_cairnwork_worker"
WGROUP="_cairnwork_worker"
RUNGROUP="_cairnwork_wrun"
ROOT="/Users/Shared/cairnwork"
RUNS="$ROOT/worker-runs"
BIN="$ROOT/bin"
SUDOERS="/etc/sudoers.d/cairnwork-worker"
PURGE="${1:-}"

[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }

# 1. sudoers first, so no rule can name a removed account even briefly
[[ -f "$SUDOERS" ]] && rm -f "$SUDOERS" && echo "removed $SUDOERS"

# 2. the worker account
if id "$WORKER" >/dev/null 2>&1; then
  dscl . -delete "/Users/$WORKER"
  echo "removed user $WORKER"
fi

# 3. the two groups
for g in "$RUNGROUP" "$WGROUP"; do
  if dscl . -read "/Groups/$g" >/dev/null 2>&1; then
    dscl . -delete "/Groups/$g"
    echo "removed group $g"
  fi
done

# 4. directories only if asked, and only when empty (never delete run data blindly)
if [[ "$PURGE" == "--purge-dirs" ]]; then
  for d in "$RUNS" "$BIN"; do
    if [[ -d "$d" ]]; then
      rmdir "$d" 2>/dev/null && echo "removed empty $d" || echo "kept non-empty $d (remove by hand)"
    fi
  done
fi

echo "rollback done. verifier account and $ROOT/runs are untouched."
