#!/bin/zsh
# One-time setup, after setup_verifier_user.sh: the Python the verifier account runs.
#
# The verifier account cannot read the owner's home, so it needs its own interpreter
# and pytest under the shared root. Files are owner-written, group-readable (0750).
# Run as the owner, without sudo: zsh scripts/setup_verifier_python.sh
set -euo pipefail

ROOT="/Users/Shared/cairnwork"
HERE="${0:A:h}"
PYTEST=$(awk '/^name = "pytest"$/ {getline; gsub(/[^0-9.]/, ""); print; exit}' "$HERE/../uv.lock")
[[ -n "$PYTEST" ]] || { echo "pytest version not found in uv.lock"; exit 1; }

umask 027
uv python install 3.13 --install-dir "$ROOT/python"
PY=$(uv python find 3.13 --managed-python --no-project 2>/dev/null | grep "^$ROOT/python/" || true)
[[ -n "$PY" ]] || PY=$(ls -d "$ROOT"/python/cpython-3.13*/bin/python3.13 | tail -1)
uv venv --allow-existing --python "$PY" "$ROOT/venv"
uv pip install --python "$ROOT/venv/bin/python" "pytest==$PYTEST"
"$ROOT/venv/bin/python" -c "import pytest, sys; print(sys.executable, 'pytest', pytest.__version__)"
