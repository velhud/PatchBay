# Phase 1 — Durable Natural-Language Codex Workers

## Outcome

Phase 1 adds one complete user-visible loop:

```text
start a named Codex worker
→ let it complete a read-only turn
→ read its engineering report
→ restart the wrapper
→ continue the same Codex conversation by worker name
→ stop an active turn when necessary
```

ChatGPT no longer needs to retain or expose a `job_id` or Codex `session_id` for this workflow.

## Deliberate Simplicity

Phase 1 does **not** add a worker database, message broker, mailbox service, transcript copy, artifact registry, role engine, or worktree manager.

A worker is derived from the durable job records the application already owns:

```text
private job options → stable worker id and name
Codex session id    → conversation continuity
job state           → current worker state
job result summary  → latest worker report
repository path     → workspace association
```

Worker jobs are exempt from automatic old-job cleanup because those records are the minimal durable identity layer. Explicit cleanup/retention policy can be added later after real use proves what is needed.

## Public Tools

- `codex_worker_start`: appoint a read-only Codex colleague with a natural-language brief.
- `codex_worker_message`: continue or redirect the same Codex conversation by worker name or id.
- `codex_worker_list`: show the available colleagues and their latest report/state.
- `codex_worker_inspect`: read one current report, optionally waiting briefly.
- `codex_worker_stop`: stop only the active turn while preserving the colleague and conversation.

## Busy Worker Rule

Phase 1 does not invent a queue. When a worker is busy, `codex_worker_message` returns a natural explanation and does not create another turn. ChatGPT can inspect later or stop the active turn before issuing a replacement direction.

## Privacy Boundary

Normal worker output omits:

- repository and worktree absolute paths;
- low-level job ids;
- Codex session ids;
- raw transcript bodies;
- raw stdout/stderr.

The existing low-level tools remain available for debugging and power-user use.

## Deferred From Phase 1

- simultaneous writing workers;
- worker-to-worker relay shortcuts;
- result comparison and integration.

Phase 2 has since delivered stable worker-owned writing worktrees and worker change/diff inspection. Integration remains deferred.
