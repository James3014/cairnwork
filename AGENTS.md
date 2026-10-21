# Cairnwork repository contract

Read `README.md` and `docs/LESSONS_FROM_NEXUS.md` first.

## Before you change anything

- Get scope from the integration owner before starting; at most two agent sessions develop Cairnwork at the same time. Find the owner in `README.md` or a CODEOWNERS file; if nothing names one, open an Issue and mention @James3014 (the maintainer) to get scope before starting.
- One worktree per session; the main clone at `~/workspace/cairnwork` is read-only.
- Do not copy Nexus-new code wholesale. Every capability enters under one of three classes (README): safeguard, experimental (`experimental/`, default off, owner + expiry), or formal (beat the bare core in Cairnwork's own A/B).
- Experimental capabilities live in `experimental/<name>/` with `__init__.py`, the module, `OWNER` and `EXPIRES` (ISO date); default off, 14-day expiry, never imported by core. Not-yet-wired code belongs there, not in `src/cairnwork`: `experimental/` still obeys the file and function limits but does not count toward the 5,000-line core cap.
- Never put the names or task ids of private evaluation task banks in public Issues, PRs or comments; refer to them only by Issue number.
- No experimental capability is moved in until the bare core passes the four drills: normal, failure, restart, rollback.
- Never modify an evaluation's measurement environment (its pinned task set, harness, models or environment) without the maintainer's explicit approval, whether or not a run is in progress.

## Rules the code must follow

- A failure in a safeguard, in verification or in a Core call must never produce a success state or an unauthorised side effect. Allowed degradations are listed in the contract, not improvised in `except` blocks.
- Every task has exactly one owning system, bound before dispatch by a durable, atomic record (idempotency key, request digest, owner, task id). Resend, query, cancel and recovery route by that record; unknown ownership stops dispatch.
- Only a nexus-core VERIFIED verdict marks a task complete.
- Limits are enforced in CI: file ≤400 lines, function ≤80 lines, `src/cairnwork` ≤5,000 lines, no import cycles, core never imports `experimental`.

## Verification

```bash
uv sync --locked
uv run ruff check . && uv run ruff format --check .
uv run lint-imports
uv run python scripts/check_size.py
uv run pytest -q
```

Run the sandbox tests for real on macOS: `CAIRNWORK_REQUIRE_SANDBOX=1 uv run pytest -q`. Without it the `sandbox`-marked tests are skipped locally, while CI's `test-macos` job sets it and fails if `sandbox-exec` is missing, so a local green run can hide a real failure. Local verification always runs the full `uv run pytest -q` (with `CAIRNWORK_REQUIRE_SANDBOX=1` on macOS). Separately, for Issue-intake tasks the verifier counts a run as passed only if every collected test passed, so a skipped test fails it: when choosing the verification commands or hidden tests of such a task, pick targets that have zero skips on the host (`scripts/env_smoke.py` reports `SKIPPED_TESTS`).

## Merging

Same as the other Nexus repos: independent review (a Claude-family reviewer other than the author; high-risk paths add a Codex or Gemini record), review record posted on the PR with head SHA and verdict, required checks green, then the author merges with `gh pr merge N --squash --match-head-commit <sha>`. PR bodies carry the NEXUS_ISSUE_INTENT_V1 block when they close an Issue.

### PR body and review record

- Every PR body carries one `Cairnwork:` line (`Cairnwork: 受影響`, `不受影響` or `尚無對應`, optionally with a short reason) and, when it closes an Issue, the `NEXUS_ISSUE_INTENT_V1` block. `Closes #N` in the body, the commit message and the intent block must agree; a custom squash message cannot fix a mismatch, so fix the commit message.
- The independent record must come from a Claude-family reviewer named in the record (or be a submitted GitHub APPROVED review) and be a different agent from the author. Post it as a PR comment whose section header names the reviewer family, with a verdict line naming the full head SHA:
  ```
  ## Independent review (Claude Opus)
  Verdict: ACCEPT@<full 40-character head sha>
  ```
- High-risk paths additionally need a cross-model record (Codex or Gemini, same format with that family in the header; at most three rounds). High risk means verification or gate logic, `.github/workflows/`, `scripts/ops/`, `scripts/ci/`, `pyproject.toml`, `uv.lock`, `ruff.toml`, `pytest.ini`, `pyrightconfig.json` and this file. Headless reviewers cannot use tools, so put the full diff in the prompt.
- A record is bound to one head SHA. Any commit that changes the PR's content needs a fresh review of the new head. Only a merge of `main` with zero diff on the PR's own paths may be carried forward, and the new record must say so: "delta since review: merge of main only, zero diff on PR paths". Re-posting an old record or pushing an empty commit does not satisfy review.
- After posting a record, wait a minute or two: `Review record present (advisory)` re-evaluates on PR comments and usually turns green by itself.
- To close an Issue, put the Issue marker and intent block in the PR body (use `KEEP_OPEN` instead of `CLOSE` to leave it open):
  ```
  Closes #N
  <!-- NEXUS_CORE_ISSUE: N -->
  <!-- NEXUS_ISSUE_INTENT_V1
  [{"issue": N, "on_merge": "CLOSE"}]
  NEXUS_ISSUE_INTENT_V1 -->
  ```

### Required checks and merging

- The ruleset requires `test (3.11)`, `test (3.12)`, `test (3.13)`, `test-macos`, `Review record present (advisory)` and `Bindings preflight (default branch)`. Other jobs (`pyright`, `hypothesis`, CodeQL) are not required but should not be red without a written disposition (pre-existing or accepted) in the review record.
- When the required checks are green, merge with `gh pr merge N --squash --match-head-commit <head sha>`. A transient 502 may still have started the merge, and an immediate retry then says it is already in progress: poll the PR state until `MERGED` instead of retrying blindly.
- After merging, delete the remote branch and remove your worktree.

### Common failure modes

- Core over 5,000 lines after another PR merged first: merge `origin/main`, re-run the checks, or move not-wired code to `experimental/`.
- Sandbox tests silently skipped locally: set `CAIRNWORK_REQUIRE_SANDBOX=1`.
- macOS temp directories under `/var/folders/...` are symlinks into `/private`: call `.resolve()` on run roots, or the sandbox sees a different real path and denies access.
- A stale `.venv` can fail with an `xcode-select`/`xcrun` error: run `uv sync --locked`, or work from a fresh worktree.

Merge on current main, not a stale base. A PR's checks run against main as it was when they last ran, but some limits are additive across PRs: the core size cap (`src/cairnwork` ≤ 5000) and any other whole-repo total. Two PRs that each pass alone can break main together — this happened when #29 and #30 each measured under 5000 but their sum was 5048, turning main red. So before you merge, if main has advanced since your checks last ran, merge `origin/main` into the branch (same rule as "whoever merges second rebases" in the other repos) and let CI re-run; the `--match-head-commit` SHA is that re-run head. `ci.yml` also runs `check_size` on every push to `main`, so a breach that slips through still turns main red at once rather than silently — but prevent it by re-running on current main first, especially when the core is near the limit.
