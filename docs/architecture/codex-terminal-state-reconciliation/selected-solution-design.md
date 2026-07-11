# Selected Solution Design

## 1. Add A Codex Session Observer

Create a focused Python component under `src/patchbay/jobs/` that:

- receives the exact session ID already discovered from Codex stdout;
- resolves only the corresponding session JSONL under the configured Codex
  home;
- incrementally tails new JSONL bytes using a retained offset;
- tolerates a partially written final line and malformed unrelated records;
- recognizes only supported terminal records, initially
  `event_msg.payload.type == task_complete`;
- retains the latest final assistant message and minimal terminal metadata;
- never returns raw transcript content to manager-facing outputs.

Resolution must not select a session merely because it is recent. If the exact
session cannot be resolved safely, the observer stays unavailable and the
existing stdout/process path continues.

## 2. Normalize Terminal Evidence

Extend the internal process capture result with additive fields such as:

- `semantic_terminal_seen`;
- `terminal_source`;
- `terminal_observed_at`;
- `session_final_message`;
- `wrapper_cleanup_required`;
- `wrapper_cleanup_outcome`.

Supported semantic sources are stdout `turn.completed`, a structured Codex
result event where contractually terminal, and exact-session `task_complete`.
Process exit remains recorded separately.

## 3. Separate Completion From Cleanup

During `_communicate_with_progress()`:

1. keep consuming stdout/stderr normally;
2. once the session ID is known, start the exact session observer;
3. when terminal evidence appears, stop waiting indefinitely for wrapper exit;
4. capture all currently available pipe output and the final session message;
5. allow a short configurable **post-completion wrapper-exit grace**;
6. terminate then kill only the lingering wrapper/process tree if required;
7. parse and persist the final report;
8. release locks and publish completed state.

The grace applies only after semantic completion. It is not a worker timeout.

## 4. Make Terminal Transitions Atomic

Add a JobManager terminal transition method guarded by its lifecycle lock:

- pending/running may transition once to completed, failed, or cancelled;
- repeated identical terminal updates are idempotent;
- later competing terminal transitions do not overwrite the first durable
  decision;
- late evidence is attached as diagnostic metadata without reopening state.

Cancellation checks state through this method before signalling a process.
Completion committed first makes cancellation a no-op. Cancellation committed
first remains cancelled even if a late session event is discovered.

## 5. Recover Persisted Jobs

Before failing a durable running job as tracking-lost, reconciliation checks:

- a persisted exact session ID exists;
- its exact session log is safely resolvable;
- strict `task_complete` evidence exists;
- a bounded final report can be recovered.

If so, PatchBay performs safe wrapper cleanup when still possible and completes
the job. It must not scan or attach another worker's session.

## 6. Preserve Existing Result Semantics

Prefer the current structured stdout result when available. When stdout lacks a
usable final result but the session has a final assistant message, pass that
message through the existing JSON/text fallback parser and redaction path.
Expose only compact provenance fields such as `result_source` and
`terminal_source`.

## 7. Keep Hub Thin

No Hub-side session parser is added. Correct Edge job state flows through the
existing worker projection into group status, wait, inspect, and close. This
keeps one lifecycle authority and avoids divergent inference.

