"""Local worker arm (#88): every tool call runs as _cairnwork_worker.

Experimental, default off, not imported by the core. tools/canary are the isolation half; agent
(the loop), endpoint (loopback chat API), episode (the corpus record) and corpus_io (the only file
I/O of the loop) are the minimal agent and its episode recorder; ``python -m
experimental.local_worker.cli`` runs or resumes one episode (knowledge addendum 2026-10-23 §0a).
Install (once, by the Owner): ``sudo zsh experimental/local_worker/install_local_worker_tool.sh
<owner>``, then ``zsh experimental/local_worker/setup_worker_python.sh``; witness with
``python -m experimental.local_worker.canary --mode worker --out <dir>``.
"""
