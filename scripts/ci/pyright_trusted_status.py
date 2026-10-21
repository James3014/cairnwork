"""Map the trusted Pyright run to a commit status (Issue #66).

Reads PYRIGHT_JOB_RESULT (the needs.<job>.result of the analysis job) and PYRIGHT_GATE_RC (the
exit code of pyright_trusted.sh, empty when the step never ran). Writes `state=` and
`description=` to $GITHUB_OUTPUT. Only a successful job with gate code 0 yields `success`;
everything else (skipped, cancelled, crash, missing output, unknown code) is not success. The
description is a fixed string per outcome: no PR-controlled text is ever echoed.
"""

from __future__ import annotations

import os
import sys

DESCRIPTIONS = {
    "success": "No new pyright findings versus the merge base",
    "failure": "New pyright findings introduced by this PR",
    "error": "Pyright gate could not run or failed closed",
}


def decide(job_result: str, gate_rc: str) -> str:
    if job_result != "success":
        return "error"
    if gate_rc == "0":
        return "success"
    if gate_rc == "1":
        return "failure"
    return "error"


def main() -> int:
    state = decide(os.environ.get("PYRIGHT_JOB_RESULT", ""), os.environ.get("PYRIGHT_GATE_RC", ""))
    lines = f"state={state}\ndescription={DESCRIPTIONS[state]}\n"
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(lines)
    print(lines, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
