# Cairnwork repository contract

Read `README.md` and `docs/LESSONS_FROM_NEXUS.md` first. Decision and council record: knowledge `nexus/decisions/2026-10-19-cairnwork-owner-decision.md` and `nexus/decisions/council-2026-10-19-cairnwork/CONSENSUS_R2.md` (clauses A–I).

## Before you change anything

- Get scope from the integration owner (`coord who repo:cairnwork`). At most two Claude sessions develop Cairnwork at the same time.
- One worktree per session; the main clone at `~/workspace/cairnwork` is read-only.
- Do not copy Nexus-new code wholesale. Every capability enters under one of three classes (README): safeguard, experimental (`experimental/`, default off, owner + expiry), or formal (beat the bare core in Cairnwork's own A/B).
- No experimental capability is moved in until the bare core passes the four drills: normal, failure, restart, rollback.
- Never touch the Nexus-new#2721 measurement environment.

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

## Merging

Same as the other Nexus repos: independent review (another Claude agent; high-risk adds Codex or Gemini), review record posted on the PR with head SHA and verdict, required checks green, then the author merges with `gh pr merge N --squash --match-head-commit <sha>`. PR bodies carry the NEXUS_ISSUE_INTENT_V1 block when they close an Issue.
