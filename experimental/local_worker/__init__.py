"""Local worker arm (#88), isolation half: every tool call runs as _cairnwork_worker.

Experimental, default off, not imported by the core. The agent loop itself is not here yet; it
waits for the PAUSE pilot (knowledge addendum 2026-10-23 §0a).
Install (once, by the Owner): ``sudo zsh experimental/local_worker/install_local_worker_tool.sh
<owner>``, then ``zsh experimental/local_worker/setup_worker_python.sh``; witness with
``python -m experimental.local_worker.canary --mode worker --out <dir>``.
"""
