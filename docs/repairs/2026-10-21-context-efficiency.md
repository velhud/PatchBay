# Context and response efficiency

This update follows the [live coding repairs](2026-10-21-live-trial-repairs.md)
at base commit `778132d793f7663ed9da387b7b830d0f6aa632b8`. It reduces routine
response overhead and clarifies how to start a real task. It does not qualify
a different ChatGPT model or turn the disposable trial into a production
deployment.

## Changes

- Local MCP uses compact JSON for small non-worker text results. Parsed text
  and full structured results remain identical, including source, errors and
  pagination. Existing large-response bounds and worker summaries remain.
- `author_recover_task` accepts a strict `operation_limit` from 1 to 200,
  defaulting to 20 recent operations. Its separate active/uncertain section,
  counts and partial flags remain. Full history is available through existing
  versioned cursor pages. CLI/domain defaults remain 50.
- A reproduced recovery defect could misclassify a task when uncertain effects
  or checks fell beyond the first 201 active rows. Classification now counts
  the entire active set in the same SQLite snapshot. The bounded projection
  prioritizes uncertainty, started checks and queued checks, with an explicit
  partial flag above 200 rows and history paging for omitted identities. Four
  regressions cover both uncertainty states and queued/running checks.
- The opt-in Author summary profile carries bounded identity, progress,
  coverage, error hints and references for full operation details. All validated
  payload content remains structured. Full compatibility output stays the
  default until an actual client proves structured source/log/error access.
- Local initialization guidance consolidates repeated polling, ownership and
  session guidance. The client guide contains one complete worker model policy.
  Protocol regressions verify that initialization instructions are not appended
  to routine tool results.
- The [heavy-task guide](../user/chatgpt-heavy-tasks.md) provides separate direct
  authoring and worker delegation prompts, Codex-led setup, explicit integration
  authority, durable recovery and current trial limits.

## Measured scope

These are UTF-8 byte measurements, not token counts or estimates of model
attention, cost or success rate.

| Measurement | Before | After | Scope |
| --- | ---: | ---: | --- |
| Local self-test text | 853 | 600 | Same deterministic payload; full structured result unchanged |
| Local worker-options text | 2,417 | 1,589 | Same deterministic payload; full structured result unchanged |
| Author recovery envelope | 42,466 | 20,580 | Same fixture with 60 completed operations and one older uncertain operation; 50 versus 20 recent rows |
| Canonical local initialization guidance | 6,811 | 6,510 | Consolidated repeated guidance; Hub remains 4,553 |
| Author profile reconstruction | 1,990,543 | 1,036,828 | Re-encoded 280 complete historical compatibility results with the current summary function; 32 truncated capture previews excluded |

The recovery comparison retained the identical active/uncertain section and
total count and exposed the shortened history as partial. The profile
reconstruction is a serializer comparison on historical payloads, not an
observed summary-profile ChatGPT trial. Its 47.91% byte reduction does not
establish client accessibility or token savings.

The four large-backlog regressions failed against the old recovery code with
`awaiting_input` in place of the required controller/resource/execution state.
They pass with the full-set classification and prioritized projection. This
changes the recovery view; it does not reconcile, retry, cancel or release an
execution fence.

## Evaluator acknowledgement ordering

The Hub V2 fairness evaluator waited for Hub-visible worker completion, then
immediately asserted that the Edge's only pending outbox receipt was the
deliberately poisoned receipt. Hub completion can become visible before the
successful response returns and the Edge records its local acknowledgement.
A controlled 600 ms response delay reproduced two completed later workers
with two pending receipts and an early evaluator failure.

The evaluator now waits for both worker completion and the exact expected
pending receipt ID. Its deadline and failure diagnostics remain. A real
loopback TCP regression exercises that response delay and passes with the
corrected barrier. This is an evaluator synchronization repair; it does not
change production worker, transport, retry or acknowledgement behavior. The
original Linux log was truncated, so it alone did not establish that race as
the cause of its failed evaluator run.

A later Linux preview captured another harness problem: the deterministic
executor had completed a process-free job but left a prior live executor-task
snapshot cached. Integration correctly stayed blocked. The deterministic
executor now records its known process-free cleanup barrier at completion and
refreshes liveness. It skips this path when launch/process identity, tracked
processes, an active cleanup owner or an existing cleanup outcome is present.
Regressions cover the stale cache and prove that a pre-existing unresolved
cleanup outcome and repository lease remain fenced. Production executor and
integration guards are unchanged.

Native instructions are returned during initialization. Repeated transport
initializations do not establish that ChatGPT repeats the entire instruction
body in its model context. The private Linux worker trial used a shorter
gateway instruction override and a scoped tool allow-list; it did not qualify
the complete canonical Hub catalog in ordinary ChatGPT. Deterministic Hub
evaluators separately exercise the complete 31-tool surface.

## Qualification boundaries

Source, schema and output profile participate in Author qualification identity.
The updated code needs a new observed client qualification before write access
on an upgraded route. The existing trial stays on its qualified source and
compatibility profile until that observation is possible. Browser control was
unavailable during this update, so no fresh ordinary-Chat A/B result or Pro
result is claimed.

The recorded ordinary-GPT-6 trials establish direct coding/checks/correction,
worker delegation/waits/review and scoped integration on disposable fixtures.
Pro compatibility, unattended Mac cleanup, all research scenarios, persistent
production ingress and physical restart/power-loss remain separately qualified
boundaries. See [TESTING.md](../../TESTING.md) for the required verification.

## Verification of this update

The complete v2 snapshot passed 1,236 tests with 39 skips on Mac and 1,268
tests with seven skips on Linux. Separate Author suites passed 183 tests with
35 skips on Mac and 213 with five skips on Linux. Critical audit regressions,
compilation, dependency checks, MCP evaluation (27 checks), its terminal
reconciliation variant (38 checks) and Hub/Edge evaluation passed.

The two evaluator files changed after that snapshot. Their final focused
five-test file and standalone 32-check Hub V2 evaluator are qualified separately
on both hosts; the other 294 Python files match the full-suite snapshot.
Prior real 185-second execution and offline Docker evidence is retained for
the unchanged execution implementation; this update does not claim a new live
client or production qualification. GitHub Actions remains disabled, so these
are recorded local and Linux runs rather than CI results.
