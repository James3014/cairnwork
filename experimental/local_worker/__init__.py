"""Local worker arm (#88): every tool call runs as _cairnwork_worker.

Experimental, default off, not imported by the core. tools/canary are the isolation half; agent
(the loop), endpoint (loopback chat API), episode (the corpus record) and corpus_io (the only file
I/O of the loop) are the minimal agent and its episode recorder; ``python -m
experimental.local_worker.cli`` runs or resumes one episode (knowledge addendum 2026-10-23 §0a).
Install (once, by the Owner): ``sudo zsh experimental/local_worker/install_local_worker_tool.sh
<owner>``, then ``zsh experimental/local_worker/setup_worker_python.sh``; witness with
``python -m experimental.local_worker.canary --mode worker --out <dir>``.

The base checkout must be exactly its HEAD commit: no changed, untracked or ignored file, and no
empty directory, FIFO or socket (the status query lists those too, as ``x`` entries, since git
does not track them). Anything else is refused at start (agent.RefusedStart) and no episode
runs. Each run root also needs a real ``q`` directory (0770) for the git queries.
"""
