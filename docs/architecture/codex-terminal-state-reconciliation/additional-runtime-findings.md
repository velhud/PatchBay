# Additional Runtime Findings

Status: `IMPLEMENTED_AND_VERIFIED`

This addendum records issues found while comparing a later real Hub work-group
report with Hub state, Edge durable job records, Codex session JSONL, process
state, and the current implementation. Private machine, repository, group,
worker, and session identifiers are intentionally omitted.

## Coverage Matrix

| Reported symptom | Verified reality | Covered by current local repair? |
|---|---|---|
| Broad read-only workers appeared stalled and were stopped | Four stopped workers had already emitted final reports and strict session `task_complete` events | Yes |
| One worker failed immediately with exit code 1 | Codex emitted an explicit usage-limit error and `turn.failed` | No; classification improvement remains |
| Test command was unavailable | The `pytest` executable was absent from PATH, but `python3 -m pytest` was installed and working | No runtime defect; guidance improvement remains |
| Group preflight still showed the old commit | Verified: stored preflight facts remained at the creation-time revision after the base checkout advanced | No |
| A completed implementation worker disappeared from Hub inspection | Verified: Edge durable state had a completed exit-0 job and report, but Hub returned worker-not-found | No |

## 1. False Stalls After Real Completion

The affected session logs contained final assistant reports followed by
`event_msg.payload.type == task_complete`. PatchBay had kept the jobs running
because the CLI wrapper stayed alive, and the manager later cancelled them.

This is the exact class repaired by the local terminal-reconciliation update:
exact-session observation, result preservation, post-completion wrapper cleanup,
atomic completion/cancellation arbitration, and restart recovery.

The evidence does not support blaming broad repository searches for these four
cases. Broad commands may still be inefficient, but the workers completed their
assigned analysis. Their reported stall was a lifecycle-observation error.

## 2. Codex Usage-Limit Failure Is Too Generic

The failed worker's bounded stdout contained a Codex `error` event and
`turn.failed` whose message explicitly said the account usage limit had been
reached and supplied a retry time. PatchBay preserved that evidence but exposed
the worker summary as only a generic exit-code-1 failure.

### Selected solution

- Extend Codex failure classification to recognize structured `turn.failed`
  usage/quota events.
- Return a compact category such as `codex_usage_limit`, a truthful public
  message, retry guidance, and a parsed retry time when one is present.
- Do not automatically retry on a more expensive model or another account.
- Preserve the raw bounded evidence only in private runtime artifacts.
- Test structured and stderr-only variants without hard-coding one exact English
  sentence.

## 3. Python Test Command Discovery

The Edge had `pytest` 9 installed as a Python module. `python3 -m pytest
--version` succeeded; only a standalone `pytest` command was absent from PATH.
The statement that the environment lacked the dependency was therefore
incorrect.

### Selected solution

- Worker guidance should treat `command not found` as a command-resolution
  question, not proof that a dependency is absent.
- Follow repository documentation first, then try the active interpreter form
  (`python -m pytest` or `python3 -m pytest`).
- If the module is truly absent, create/reuse the documented repo-local virtual
  environment and install required development dependencies on the dedicated
  workbench.
- Report the exact command attempted and exact failure rather than saying only
  that tests were unavailable.

This is primarily worker/manager instruction quality, not a PatchBay execution
failure.

## 4. Stored Group Preflight Becomes Stale

The group's stored preflight continued to advertise its creation-time HEAD even
after the base checkout was safely fast-forwarded and verified at a newer
revision. Current group status presents those facts without a sufficiently
strong snapshot/currentness distinction.

### Selected solution

- Add `observed_at`, `facts_revision`, and `currentness` to group readiness.
- Label preflight facts explicitly as a snapshot, never as live repository state.
- Mark readiness `refresh_required` after a base integration, explicit base
  mutation, group resume, or newer worker/integration activity that can make the
  snapshot obsolete.
- Use the existing group-resume operation as the explicit refresh boundary
  rather than adding another public tool solely for preflight refresh.
- Update manager instructions: refresh/resume before a new implementation phase
  when the base checkout changed.
- Add tests for fetch/fast-forward, worker integration, same-group continuation,
  and stale snapshot display.

## 5. Edge Projection Can Stop Advancing Silently

The most serious new issue is independent of Codex completion:

- Hub machine heartbeats remained fresh.
- Hub's full worker projection revision and receipt time stopped advancing.
- A completed implementation worker existed in Edge durable state with exit
  code 0, final report, session, and isolated worktree.
- Hub could not inspect that worker and returned worker-not-found.
- Building a projection from a copied version of the same durable state
  completed successfully in about a fraction of a second, included every worker,
  included the missing worker, and produced a modest payload.
- The live Edge keeps background projection errors only in process memory and
  does not expose or log them, so the exact live-loop exception cannot be
  recovered after the fact.

This narrows the failure to the live projection loop or its live in-memory state,
not the durable worker data, projection size, or Hub routing.

### Selected solution

1. Supervise every Edge control loop independently. A cancelled, crashed, or
   unexpectedly returned projection task must be restarted with bounded backoff.
2. Persist and expose compact projection health:
   `last_attempt_at`, `last_success_at`, `last_success_revision`,
   `consecutive_failures`, `last_error_category`, and `projection_age_seconds`.
3. Log background-loop failures through the normal private runtime logger;
   retaining them only in an in-memory list is insufficient.
4. Build projections from an immutable job snapshot under the JobManager state
   lock so concurrent job creation/completion cannot invalidate iteration.
5. Isolate one malformed worker projection: emit a safe worker-level projection
   error and keep the full snapshot contract explicit rather than killing the
   entire projection loop silently.
6. Hub must mark machine worker state as `projection_stale` when heartbeats are
   fresh but projection age exceeds policy. It must not present stale worker
   counts as current.
7. Worker inspect should route to the pinned Edge using durable fleet-worker
   identity when a group/operation knows the worker but the cached projection is
   stale. A missing cache record must not erase a real Edge worker.
8. Test more than the historical worker count observed in production,
   concurrent starts/completions during snapshot creation, deliberate projection
   build failure, HTTP publication failure, loop cancellation, automatic loop
   recovery, stale-projection status, and direct inspect fallback.
9. Extend the outside-in Hub evaluation to prove that a newly completed worker
   remains listable and inspectable after many historical workers exist.

## Current Runtime State At Investigation Time

- Both enrolled machines were online and compatible.
- Both reported zero active workers and all configured slots free.
- The relevant work group remained open.
- The named implementation worker had completed normally and retained its
  isolated changes, but Hub inspection had lost it because of stale projection.
- No live Codex execution processes remained on the pinned Edge; only old zombie
  helper entries were visible.

No service was restarted, no worker was stopped, no repository was changed, and
no deployment was performed during this investigation.

## Implemented Resolution

The release following this investigation implemented the related fixes as one
coherent lifecycle update:

- exact-session terminal reconciliation prevents completed Codex turns from
  remaining falsely active behind a lingering CLI wrapper;
- structured usage-limit failures are exposed as `codex_usage_limit` with
  retry guidance instead of a generic exit-code failure;
- successful group preflight stores `observed_at`, `facts_revision`, and
  `currentness`, while group resume remains the explicit strict refresh boundary;
- every Edge control loop is independently supervised and restarted with
  bounded backoff if it is cancelled, crashes, or returns unexpectedly;
- compact loop health is durable in the Edge journal, including attempt and
  success times, successful projection revision, consecutive failures, safe
  error category, and restart count;
- heartbeat telemetry carries projection health, and Hub fleet output labels
  worker projection state as `current`, `stale`, `failed`, or `unknown` rather
  than presenting old counts as live truth;
- background control failures are written to the private runtime logger instead
  of existing only in volatile process memory.

Verification included a forced cancellation of the projection child while the
Edge remained online. The supervisor restarted projection publication, advanced
the durable revision, retained heartbeat operation, and persisted restart
evidence. Full unit/regression and outside-in MCP/Hub evaluations passed.
