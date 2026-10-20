#!/bin/zsh
# Build the Python environment the verifier account uses for one target repository.
#
# Usage (as the owner, without sudo, after setup_verifier_python.sh):
#     zsh scripts/setup_target_venv.sh <clean clone of the target repo> <name> [python version]
#
# The Python version defaults to 3.13; use the one the target's CI uses (Nexus-new: 3.12).
# Installs exactly what the target's uv.lock pins (all groups and extras, without the
# project itself: the verifier imports it from the checkout under test) into
# /Users/Shared/cairnwork/venvs/<name>, plus pytest if the lock has none. Files are
# owner-written and group-readable (umask 027), so the account can run but not change
# them. Point the intake at it with config.json {"verifier_python": ".../bin/python"}.
# Rebuild after the target's lock changes; a missing package makes verifiers fail.
set -euo pipefail

ROOT="/Users/Shared/cairnwork"
CLONE="${1:?usage: setup_target_venv.sh <repo clone> <name>}"
NAME="${2:?usage: setup_target_venv.sh <repo clone> <name>}"
PYVER="${3:-3.13}"
[[ "$PYVER" =~ '^3\.[0-9]+$' ]] || { echo "bad python version: $PYVER"; exit 1; }
[[ "$NAME" =~ '^[A-Za-z0-9][A-Za-z0-9_.-]*$' ]] || { echo "bad name: $NAME"; exit 1; }
[[ -f "$CLONE/uv.lock" ]] || { echo "$CLONE has no uv.lock"; exit 1; }
VENV="$ROOT/venvs/$NAME"
WANT=$(awk -F'"' '/^requires-python/ {print $2; exit}' "$CLONE/pyproject.toml")
echo "target requires-python: ${WANT:-unknown}"

umask 027
mkdir -p "$ROOT/venvs"
uv python install "$PYVER" --install-dir "$ROOT/python"
PY=$(ls -d "$ROOT"/python/cpython-"$PYVER".*/bin/python"$PYVER" | tail -1)
[[ -x "$PY" ]] || { echo "no Python $PYVER under $ROOT/python"; exit 1; }
REQS=$(mktemp -t cairnwork-reqs)
trap 'rm -f "$REQS"' EXIT
uv export --directory "$CLONE" --frozen --all-groups --all-extras --no-hashes \
    --no-emit-project --no-header --no-annotate --format requirements-txt > "$REQS"
uv venv --allow-existing --python "$PY" "$VENV"
uv pip install --python "$VENV/bin/python" -r "$REQS"
"$VENV/bin/python" -c "import pytest" 2>/dev/null || uv pip install --python "$VENV/bin/python" pytest
"$VENV/bin/python" -I -c "import pytest, sys; print(sys.executable, 'pytest', pytest.__version__)"
