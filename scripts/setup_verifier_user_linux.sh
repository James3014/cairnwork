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
OWNER_UID="$(id -u "$OWNER")"

# If the account name already exists, make sure it is ours and unprivileged before reusing it:
# a pre-existing name that maps to the owner's uid (candidate would run as the owner) or to root,
# has a login shell, or carries extra group memberships (a privileged or secret-reading group)
# would defeat the boundary. Refuse rather than trust the name. (The runtime also refuses a uid
# of 0 or the owner's, but the installer should not silently adopt a hijacked account.)
if id "$VERIFIER" >/dev/null 2>&1; then
  v_uid="$(id -u "$VERIFIER")"
  v_shell="$(getent passwd "$VERIFIER" | cut -d: -f7)"
  { [ "$v_uid" -ne 0 ] && [ "$v_uid" -ne "$OWNER_UID" ]; } \
    || { echo "existing user $VERIFIER has uid $v_uid (root or the owner); refusing" >&2; exit 1; }
  # Linux allows two login names to share one uid; running as a name that shares another service
  # account's uid would let candidate code read that account's 0600 files (same uid, same access).
  shared="$(getent passwd | awk -F: -v u="$v_uid" -v n="$VERIFIER" '$3==u && $1!=n {print $1}')"
  [ -z "$shared" ] \
    || { echo "existing user $VERIFIER shares uid $v_uid with: $shared; refusing" >&2; exit 1; }
  case "$v_shell" in
    */nologin | /bin/false | /usr/bin/false) : ;;
    *) echo "existing user $VERIFIER has a login shell ($v_shell); refusing" >&2; exit 1 ;;
  esac
  extra="$(id -Gn "$VERIFIER" | tr ' ' '\n' | grep -vx "$GROUP" || true)"
  [ -z "$extra" ] \
    || { echo "existing user $VERIFIER is in unexpected groups: $extra; refusing" >&2; exit 1; }
fi

# The sandbox binary the account is allowed to run (the sudoers rule names it, and the
# remove()/chmod path runs through it). Resolve symlinks so the rule names the same absolute
# path the runtime invokes (verifier_account.py runs os.path.realpath(which("bwrap"))); without
# this, sudo would deny the realpath'd command on a distro where /usr/bin/bwrap is a symlink.
BWRAP="$(command -v bwrap || true)"
[ -n "$BWRAP" ] || { echo "bwrap not found on PATH; install bubblewrap first" >&2; exit 1; }
BWRAP="$(readlink -f "$BWRAP")"
case "$BWRAP" in
  /*) : ;;
  *) echo "could not resolve an absolute path to bwrap" >&2; exit 1 ;;
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

# The account path does not mask the owner's home credentials (a different uid cannot enter a
# 0700 home, and the uid boundary denies them). If the owner's home is traversable by others,
# a world-readable secret under it would be readable by the verifier — warn so the operator
# tightens it. Correctly-private secrets (0600/0700) stay protected regardless.
OWNER_HOME="$(getent passwd "$OWNER" | cut -d: -f6)"
if [ -n "$OWNER_HOME" ] && [ -d "$OWNER_HOME" ]; then
  perm="$(stat -c '%a' "$OWNER_HOME" 2>/dev/null || echo 700)"
  case "$perm" in
    *[1-7][0-9] | *[0-9][1-7]) echo "WARNING: $OWNER_HOME is group/other-accessible ($perm);" \
      "world-readable secrets under it would be readable by $VERIFIER. Prefer chmod 700." >&2 ;;
  esac
fi

echo "verifier account: $(id "$VERIFIER")"
echo "shared root:      $(ls -ld "$ROOT")"
echo "sudo rule:        $(cat "$SUDOERS")"
echo "owner $OWNER is now in group $GROUP (new processes only; use 'sg $GROUP' in the same session)"
echo "next (as $OWNER, no sudo): CAIRNWORK_VERIFIER_ROOT=$ROOT sh scripts/setup_verifier_python.sh"
