#!/bin/zsh
# One-time setup: a dedicated, unprivileged macOS account that runs Cairnwork verifiers.
#
# Why (Issue #9): inside Seatbelt, code can still read the arguments of other processes
# run by the same user (sysctl KERN_PROCARGS2). Running verifiers as a different uid
# stops that, and lets Cairnwork kill everything a verifier left behind.
#
# Run once, as the owner, with: sudo zsh scripts/setup_verifier_user.sh <owner-username>
# It is idempotent: running it again changes nothing that is already in place.
set -euo pipefail

OWNER="${1:?usage: sudo zsh setup_verifier_user.sh <owner-username>}"
VERIFIER="cairnwork-verifier"
GROUP="cairnwork"
ROOT="/Users/Shared/cairnwork"
SUDOERS="/etc/sudoers.d/cairnwork-verifier"

[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
id "$OWNER" >/dev/null

# a hidden service id below 500 that no user or group uses yet
free_id() {
  local id=$(( $(dscl . -list "$1" "$2" | awk '{print $2}' | sort -n | awk '$1<500' | tail -1) + 1 ))
  (( id < 500 )) || { echo "no free id below 500 in $1" >&2; exit 1; }
  dscl . -list "$1" "$2" | awk '{print $2}' | grep -qx "$id" && { echo "id $id in use" >&2; exit 1; }
  echo "$id"
}

# 1. group shared by the owner and the verifier account
if ! dscl . -read "/Groups/$GROUP" >/dev/null 2>&1; then
  gid=$(free_id /Groups PrimaryGroupID)
  dscl . -create "/Groups/$GROUP"
  dscl . -create "/Groups/$GROUP" PrimaryGroupID "$gid"
fi
GID=$(dscl . -read "/Groups/$GROUP" PrimaryGroupID | awk '{print $2}')

# 2. hidden account with no shell, no home and no password login
if ! id "$VERIFIER" >/dev/null 2>&1; then
  uid=$(free_id /Users UniqueID)
  dscl . -create "/Users/$VERIFIER"
  dscl . -create "/Users/$VERIFIER" UniqueID "$uid"
  dscl . -create "/Users/$VERIFIER" PrimaryGroupID "$GID"
  dscl . -create "/Users/$VERIFIER" UserShell /usr/bin/false
  dscl . -create "/Users/$VERIFIER" NFSHomeDirectory /var/empty
  dscl . -create "/Users/$VERIFIER" RealName "Cairnwork verifier"
  dscl . -create "/Users/$VERIFIER" IsHidden 1
  dscl . -create "/Users/$VERIFIER" Password "*"
fi
dseditgroup -o edit -a "$OWNER" -t user "$GROUP"
dseditgroup -o edit -a "$VERIFIER" -t user "$GROUP"

# 3. shared root: only the owner writes, the group may enter. Cairnwork creates each
#    run's directory under runs/ and opens up only its run/ scratch to the group.
mkdir -p "$ROOT/runs"
chown "$OWNER:$GROUP" "$ROOT" "$ROOT/runs"
chmod 2750 "$ROOT" "$ROOT/runs"

# 4. the owner may run, as the verifier account: sandbox-exec (any profile, so in
#    effect any code: the account protects the owner from verifiers, not the reverse),
#    kill -9 -1 (all of the account's own processes) and rm (the account's files).
mkdir -p /etc/sudoers.d
cat > "$SUDOERS.tmp" <<EOF
$OWNER ALL=($VERIFIER) NOPASSWD: /usr/bin/sandbox-exec, /bin/kill -9 -1, /bin/rm
EOF
chmod 0440 "$SUDOERS.tmp"
visudo -cf "$SUDOERS.tmp" >/dev/null
mv "$SUDOERS.tmp" "$SUDOERS"

echo "verifier account: $(id $VERIFIER)"
echo "shared root:      $(ls -ld $ROOT)"
echo "sudo rule:        $(cat $SUDOERS)"
echo "next (as $OWNER, no sudo): zsh scripts/setup_verifier_python.sh"
