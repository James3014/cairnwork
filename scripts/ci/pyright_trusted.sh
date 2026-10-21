#!/usr/bin/env bash
# Trusted Pyright gate runner (Issue #66). Runs from the default branch, never from the PR.
#
# Usage: pyright_trusted.sh TRUSTED_DIR HEAD_DIR BASE_SHA WORK_DIR
#   TRUSTED_DIR  checkout of the default branch (scripts, lockfile, gate come from here)
#   HEAD_DIR     full clone of the PR head, treated as DATA ONLY (pyright reads it, nothing in
#                it is imported, installed or executed by us)
#   BASE_SHA     tip of the PR's base branch
#   WORK_DIR     scratch directory (merge-base worktree, JSON reports)
# Exit: 0 gate passed, 1 new findings, 2 gate failed closed, 3 pyright or setup crashed.
set -uo pipefail
trusted=$(cd "$1" && pwd)
head=$(cd "$2" && pwd)
base_sha=$3
work=$(mkdir -p "$4" && cd "$4" && pwd)

mb=$(git -C "$head" merge-base "$base_sha" HEAD) || { echo "no merge base"; exit 3; }
echo "merge base: $mb"
git -C "$head" worktree add --detach "$work/base" "$mb" >/dev/null || exit 3

# The interpreter and every dependency come from the TRUSTED lockfile only.
(cd "$trusted" && uv sync --locked) || { echo "uv sync of trusted tree failed"; exit 3; }
py="$trusted/.venv/bin/python"

run_pyright() { # dir out.json
  local rc=0
  (cd "$1" && uvx --from pyright==1.1.414 pyright --pythonpath "$py" --outputjson > "$2") || rc=$?
  if [ "$rc" -gt 1 ]; then echo "pyright exited $rc in $1"; return 1; fi
}
run_pyright "$work/base" "$work/base.json" || exit 3
run_pyright "$head" "$work/head.json" || exit 3

# Gate script and Python come from the trusted tree; the head is only a path argument.
"$py" "$trusted/scripts/pyright_gate.py" "$work/base.json" "$work/base" "$work/head.json" "$head"
exit $?
