#!/bin/sh
# One-time setup: a dedicated, unprivileged Linux account that runs Cairnwork verifiers
# (Issue #84, the Linux counterpart of scripts/setup_verifier_user.sh on macOS).
#
# Why: the Linux bwrap backend binds the whole root read-only, so anything the running
# account can read is readable by candidate code. Running verifiers as a different,
# unprivileged uid makes the readable set that account's, which owns nothing, and lets
# Cairnwork kill everything a verifier left behind (kill -9 -1 as that account).
#
# Run once, as root: sudo sh scripts/setup_verifier_user_linux.sh <owner-username>
# Idempotent: running it again changes nothing already in place.
set -eu

OWNER="${1:?usage: sudo sh setup_verifier_user_linux.sh <owner-username>}"
VERIFIER="cairnwork-verifier"
GROUP="cairnwork"
ROOT="/var/lib/cairnwork"
SUDOERS="/etc/sudoers.d/cairnwork-verifier"

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
id "$OWNER" >/dev/null

# The sandbox binary the account is allowed to run (the sudoers rule names it, and the
# remove()/chmod path runs through it). Must be an absolute path.
BWRAP="$(command -v bwrap || true)"
case "$BWRAP" in
  /*) : ;;
  *) echo "bwrap not found on PATH; install bubblewrap first" >&2; exit 1 ;;
esac

# 1. group shared by the owner and the verifier account
getent group "$GROUP" >/dev/null || groupadd --system "$GROUP"

# 2. system account: no login shell, no home, no password
if ! id "$VERIFIER" >/dev/null 2>&1; then
  useradd --system --gid "$GROUP" --no-create-home \
          --home-dir /nonexistent --shell /usr/sbin/nologin "$VERIFIER"
fi
usermod -aG "$GROUP" "$OWNER"

# 3. shared root: only the owner writes, the group may enter. Cairnwork creates each
#    run's directory under runs/ and opens up only its run/ scratch to the group.
mkdir -p "$ROOT/runs"
chown "$OWNER:$GROUP" "$ROOT" "$ROOT/runs"
chmod 2750 "$ROOT" "$ROOT/runs"

# 4. the owner may run, as the verifier account: bwrap (any policy, so in effect any code:
#    the account protects the owner from verifiers, not the reverse), kill -9 -1 (all of the
#    account's own processes) and rm (the account's files). chmod during cleanup runs through
#    bwrap, so the bwrap rule covers it.
mkdir -p /etc/sudoers.d
cat > "$SUDOERS.tmp" <<EOF
$OWNER ALL=($VERIFIER) NOPASSWD: $BWRAP, /bin/kill -9 -1, /bin/rm
EOF
chmod 0440 "$SUDOERS.tmp"
visudo -cf "$SUDOERS.tmp" >/dev/null
mv "$SUDOERS.tmp" "$SUDOERS"

echo "verifier account: $(id "$VERIFIER")"
echo "shared root:      $(ls -ld "$ROOT")"
echo "sudo rule:        $(cat "$SUDOERS")"
echo "owner $OWNER is now in group $GROUP (new processes only; use 'sg $GROUP' in the same session)"
echo "next (as $OWNER, no sudo): CAIRNWORK_VERIFIER_ROOT=$ROOT sh scripts/setup_verifier_python.sh"
