#!/bin/sh
# Cairnwork installer — a convenience fallback for hosts without uv. The main path is a single
# documented command (see docs/QUICKSTART.md); prefer it, and read this script before running it.
#
#   sh install.sh                 # install uv if missing, then install the cairnwork CLI
#
# It installs: uv (from https://astral.sh/uv, only if `uv` is not already on PATH), then the
# cairnwork CLI with `uv tool install`. It never uses sudo and sets up no verifier account
# (that is a separate, documented step for running untrusted third-party code).
set -eu

REPO="${CAIRNWORK_REPO:-https://github.com/James3014/cairnwork.git}"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found — installing it from https://astral.sh/uv (no sudo)…"
  # Review https://astral.sh/uv/install.sh yourself if you prefer; this is Astral's installer.
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # uv puts itself on PATH for new shells; make it available to the rest of this run too.
  for dir in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
    [ -x "$dir/uv" ] && PATH="$dir:$PATH"
  done
fi

command -v uv >/dev/null 2>&1 || { echo "uv is still not on PATH; open a new shell and re-run" >&2; exit 1; }

echo "installing the cairnwork CLI from $REPO…"
uv tool install --from "git+$REPO" cairnwork

echo
echo "Installed. Try the five-minute demo (no GitHub, no AI worker, no API key):"
echo "    cairnwork demo"
echo "Then see docs/QUICKSTART.md to point Cairnwork at a real repository."
