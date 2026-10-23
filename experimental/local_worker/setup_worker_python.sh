#!/bin/zsh
# The interpreter the local worker's run venvs are built from (Cairnwork #88). The worker account
# cannot read the Owner's home (and the wrapper's Seatbelt profile denies /Users except the run root
# and this tree), so it needs its own Python under the shared worker root.
# Run as the Owner, without sudo, after install_local_worker_tool.sh:
#   zsh experimental/local_worker/setup_worker_python.sh
set -euo pipefail

PYROOT="${CAIRNWORK_LOCAL_WORKER_PYROOT:-/Users/Shared/cairnwork-worker/python}"
[[ -d "$PYROOT" && ! -L "$PYROOT" && -O "$PYROOT" ]] || {
  echo "$PYROOT missing or not yours (run install_local_worker_tool.sh first)" >&2; exit 1; }
umask 022
uv python install 3.13 --install-dir "$PYROOT"
PY=$(ls -d "$PYROOT"/cpython-3.13*/bin/python3.13 | tail -1)
"$PY" -c "import sys; print('LOCAL_WORKER_PYTHON', sys.executable, sys.version.split()[0])"
