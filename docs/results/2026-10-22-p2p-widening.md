# P2P widening for deferred acceptance (#117): rules, failure paths, cost

Continues the `widen-p2p` work of b063 (selector `bdd8de0`, config keys `b561b7a`), now wired into
the verification path. Semantics approved by James 2026-10-22 (knowledge/decisions; `DECISIONS.md`
section "#117 P2P 預設範圍").

## What runs

For a task with a deferred `test_commit` (worker path and `cairnwork-verify`), after the worker is
done:

1. select existing test files on the base by `p2p_selection` (state config, one state per target
   repository): `whole-suite` (default), `module-dir`, or `off` (the per-repository opt-out);
2. run them on the **base** to find the tests that pass there (a test that fails or is skipped on
   the base is not a regression); the deferred test files themselves are excluded;
3. run exactly those base-passing tests on the judged commit as one more verifier
   `p2p-<applied mode>`; Core must see it pass like any other verifier.

`module-dir`: the directories holding a test for a changed file (by name `test_<stem>.py` /
`<stem>_test.py`, or by the mirrored package directory); every test directly in such a directory
is selected. On a flat layout `tests/test_base.py` therefore brings in `tests/test_sibling.py`
(the regression of the 2026-10-22 measurement); before, only the name-matched file ran.

## Failure paths (none can end VERIFIED)

| Situation | Result |
|---|---|
| base probe times out (or reports nothing usable) in `whole-suite` | degrade once to `module-dir`; recorded as `degraded: base-probe-timeout` / `base-probe-failed` |
| `module-dir` (or the degraded run) cannot be probed either | task REFUSED, `P2P_BASE_PROBE_UNUSABLE` |
| degraded to `module-dir` and it selects no test file, or none of them passes on the base | task REFUSED, `P2P_BASE_PROBE_UNUSABLE` (a failed probe is never "nothing to run") |
| candidate P2P run exceeds the budget | the observation is not a pass, Core says not verified (no re-run) |
| a P2P run fails on the candidate only because a test failed | re-run once; a pass on the re-run is what Core sees and the first run is recorded (`rerun`: failed tests, returncode, timed_out, mutated, survivors, ran; bound into the re-run's artifact hash); a second failure blocks |
| a P2P run fails for any other reason (checkout mutated, surviving process, oversized log, no usable guard report) | blocks as it is, no re-run |
| no base-passing test selected (no degradation) | only the author's verifiers run; recorded (`targets: 0`) |
| author verifier id equals `p2p-<mode>` | REFUSED, `P2P_VERIFIER_ID_CLASH` |

`p2p_time_budget_seconds` (default 300) bounds each probe or run, so the worst case adds three
budgets (probe, degraded probe, candidate run), four when a failing candidate run is re-run. Tasks without a `test_commit` are unchanged.

## Where the rule is recorded

Core's notarisation contract is unchanged. The rule (`configured`, `applied`, `degraded`, budget,
counts, `targets_sha256`) is a JSON note bound into the p2p observation's artifact hash (so the
Core record binds it), written as `p2p-selection.json` beside the p2p verifier log, and returned as
`p2p` by `cairnwork-verify` and in the task result. The exact test list is also part of the hashed
argv.

## Cost (measured 2026-10-22, M5, `scripts/measure_p2p.py`)

One candidate that appends a marker to a source file, one deferred test; seconds for the whole
`cairnwork-verify` call, no verifier account, Seatbelt sandbox.

| Repository | tests | off | module-dir | whole-suite |
|---|---|---|---|---|
| demo fixture (`cairnwork demo` layout, 2 tests) | 2 | 1.5 s | 2.5 s | 2.4 s |
| more-itertools (`ab-repos`, 2 flat test files, ~9,000 tests) | ~9,000 | 4.0 s | 23.8 s | 23.7 s |

Reading: the added cost is about one extra pytest run on the base plus one on the candidate
(about 20 s for ~9,000 fast tests). more-itertools has a flat test directory, so `module-dir`
selects the same two files as `whole-suite`; the two modes differ only on layouts with nested test
directories. Single runs, one machine; a suite with slow tests is bounded by the budget, not by
this number. Not measured: a repository whose whole suite exceeds the 300 s budget (the timeout
and degrade paths are covered by tests with a 5 to 10 s budget instead).

## For the third A/B round (to bb)

Pre-register the two catch rates **separately**: the regression class (P2P: a change that breaks
an already passing test) and the F2P class (the deferred tests fail). If the ground truth
includes whole-suite regressions, the regression catch rate is close to 100% by construction
under the new default and must not be pooled with the F2P rate. The harness passes its P2P list
explicitly through the manifest, so changing the product default does not alter the frozen
measurement.
