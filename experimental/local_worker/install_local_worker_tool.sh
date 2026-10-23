#!/bin/zsh
# Install the local worker arm's tool wrapper and its sudoers rule (Cairnwork #88, isolation half).
#
# Run once, by the Owner (James), after scripts/setup_worker_user.sh has created the worker account:
#   sudo zsh experimental/local_worker/install_local_worker_tool.sh <owner-username>
# Undo:
#   sudo zsh experimental/local_worker/install_local_worker_tool.sh --rollback
#
# What it does (idempotent; re-run whenever local-worker-tool changes):
#   * ROOT/local-runs   owner:_cairnwork_wrun 2770  per-run directories (32-hex names) the Owner creates
#   * ROOT/python       owner:wheel 0755           the worker's interpreter, filled by
#                                                    setup_worker_python.sh (as the Owner, no sudo)
#   * BIN/local-worker-tool  owner, 0755           copy of ./local-worker-tool
#   * /etc/sudoers.d/cairnwork-local-worker (root:wheel 0440, checked with visudo -c):
#       <owner> ALL=(_cairnwork_worker) NOPASSWD: BIN/local-worker-tool
# It never touches the agy wrapper (BIN/agy-worker-exec), its rule (/etc/sudoers.d/cairnwork-worker)
# or worker-runs. Any failure removes the new rule, so nothing half-installed is ever enabled.
#
# Trust: like agy-worker-exec, BIN and the wrapper copy are Owner-owned; anything running as the
# Owner could replace the wrapper, which is in-model (the rule only lets the Owner start the worker).
set -euo pipefail

WORKER="_cairnwork_worker"
RUNGROUP="_cairnwork_wrun"
ROOT="/Users/Shared/cairnwork-worker"
BIN="$ROOT/bin"
RUNS="$ROOT/local-runs"
PYROOT="$ROOT/python"
WRAPPER="$BIN/local-worker-tool"
SUDOERS="/etc/sudoers.d/cairnwork-local-worker"
SOURCE="${0:A:h}/local-worker-tool"

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
die() { echo "LOCAL_WORKER_TOOL_INSTALL_REFUSED: $*" >&2; exit 1; }

if [[ "${1:-}" == "--rollback" ]]; then
  rm -f "$SUDOERS" "$WRAPPER"
  echo "LOCAL_WORKER_TOOL_ROLLED_BACK (rule and wrapper removed; $RUNS and $PYROOT left in place)"
  exit 0
fi

OWNER="${1:?usage: sudo zsh install_local_worker_tool.sh <owner-username> | --rollback}"
[[ "$OWNER" =~ '^[a-z_][a-z0-9_-]*$' && "$OWNER" != root ]] || die "bad owner '$OWNER'"
id "$OWNER" >/dev/null || die "no such user $OWNER"
id "$WORKER" >/dev/null 2>&1 || die "$WORKER missing (run scripts/setup_worker_user.sh first)"
dscl . -read "/Groups/$RUNGROUP" >/dev/null 2>&1 || die "$RUNGROUP missing (setup_worker_user.sh)"
for g in $(id -Gn "$WORKER"); do
  [[ "$g" != staff && "$g" != admin ]] || die "$WORKER is in $g"
done

TMP_SUDOERS="$SUDOERS.tmp.$$"
trap 'rc=$?; rm -f "$TMP_SUDOERS"; if (( rc != 0 )); then rm -f "$SUDOERS"; echo "install failed: local worker rule removed" >&2; fi' EXIT

# ROOT must be a real root-owned directory (only root can then replace what is beneath it).
[[ ! -L "$ROOT" && -d "$ROOT" && "$(stat -f %Su "$ROOT")" == root ]] || die "$ROOT is not root-owned"
[[ ! -L "$BIN" && -d "$BIN" && "$(stat -f %Su "$BIN")" == "$OWNER" ]] || die "$BIN not owner-owned"
[[ "$(stat -f %Lp "$BIN")" == 755 ]] || die "$BIN mode is not 755"
[[ -f "$SOURCE" && ! -L "$SOURCE" ]] || die "wrapper source missing: $SOURCE"
sh -n "$SOURCE" || die "wrapper syntax"

# mkdir without -p: fails if something was planted there; a planted symlink is refused below.
make_dir() {   # $1 path  $2 group  $3 mode (4 digits)
  [[ -e "$1" || -L "$1" ]] || mkdir "$1"
  [[ ! -L "$1" && -d "$1" ]] || die "$1 is not a real directory"
  chown "$OWNER:$2" "$1"
  chmod "$3" "$1"
  [[ "$(stat -f '%Su:%Sg:%Mp%Lp' "$1")" == "$OWNER:$2:$3" ]] || die "$1 has the wrong owner/mode"
}
make_dir "$RUNS" "$RUNGROUP" 2770
make_dir "$PYROOT" wheel 0755

tmp="$WRAPPER.tmp.$$"
rm -f "$tmp"
cp "$SOURCE" "$tmp"
chown "$OWNER:wheel" "$tmp"
chmod 0755 "$tmp"
mv -f "$tmp" "$WRAPPER"
[[ ! -L "$WRAPPER" ]] && cmp -s "$SOURCE" "$WRAPPER" || die "installed wrapper differs"

print -r -- "$OWNER ALL=($WORKER) NOPASSWD: $WRAPPER" > "$TMP_SUDOERS"
chown root:wheel "$TMP_SUDOERS"
chmod 0440 "$TMP_SUDOERS"
visudo -cf "$TMP_SUDOERS" >/dev/null || die "visudo rejected the rule"
mv -f "$TMP_SUDOERS" "$SUDOERS"
visudo -c >/dev/null || die "sudoers no longer parses"

# Witness: the worker can start the wrapper, and the wrapper refuses a bogus run root.
set +e
out=$(sudo -n -u "$OWNER" sudo -n -u "$WORKER" "$WRAPPER" list /nonexistent . 2>&1)
rc=$?
set -e
[[ $rc -eq 64 && "$out" == *refusing* ]] || die "witness: expected a wrapper refusal, got rc=$rc: $out"
echo "LOCAL_WORKER_TOOL_INSTALLED $WRAPPER"
shasum -a 256 "$WRAPPER"
echo "next (as $OWNER, no sudo): zsh experimental/local_worker/setup_worker_python.sh"
