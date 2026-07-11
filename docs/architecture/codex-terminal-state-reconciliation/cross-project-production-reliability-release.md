# Cross-Project Production Reliability Release

Status: `IMPLEMENTED_LOCALLY_VERIFIED_PENDING_RELEASE`

## Evidence Scope

This release was derived from current Hub journal traces, durable Hub entity
records, Edge worker projections, container process state, and reports from
several concurrent production project runs.

The evidence separated deliberate safety behavior from defects:

- one base checkout intentionally has one `shared_write` owner;
- stale integration tokens are intentionally rejected;
- null worker activity timestamps and null optional trees were incorrectly
  rejected by Hub output validation;
- a workspace root plus child repo path was incorrectly resolved to the root;
- production workspace discovery was not wired through the Hub composition;
- completed Codex turns could leave zombie wrappers, which `kill(pid, 0)`
  incorrectly treated as live;
- manager stop could race completion and replace recoverable final evidence;
- unscoped aggregate worker status could include unrelated groups;
- isolated workers shared a logical repository id without a distinct public
  execution-workspace identity;
- some connector calls did not expose structured results to ChatGPT even though
  Hub handled the operation.

## Implemented Contract

1. Multiple `shared_write` workers in one batch are rejected before dispatch.
2. Accepted checkout mutations invalidate cached preflight currentness.
3. Stale integration-token results provide an explicit fresh-preview retry.
4. Worker timestamps and optional workspace trees accept legitimate nulls.
5. `workspace_ref` plus `repo_path` binds to the exact projection or child path;
   conflicts are rejected.
6. Hub performs bounded live workspace discovery on eligible Edges.
7. Zombie processes are not live runtime evidence.
8. Exact terminal-session recovery precedes cancellation and preserves reports.
9. A trusted persisted process can be stopped after an in-memory tracking loss.
10. Hub list/status/wait calls require `work_group_id`.
11. Worker projections expose a distinct `workspace_instance_id`.
12. Startup tools provide bounded identifier-rich text fallbacks while retaining
    full `structuredContent`.
13. Rolling upgrades allow an older Edge to finish attempts already fenced to
    its advertised contract while preventing new placement on that incompatible
    Edge until it is upgraded.
14. Historical reconciliation requests authenticate the enrolled machine and
    generation, then use the durable operation/attempt/contract/fencing tuple;
    they do not become unrecoverable merely because the Edge later advertises a
    different contract.

## Verification Evidence

- Focused connected suite: 96 passed.
- Full suite: 642 passed; four existing FastAPI lifespan deprecation warnings.
- Hub V2 live evaluation: passed with exactly 31 tools, two Edges, group pinning,
  worker continuation, isolated write, integration, preflight invalidation,
  durable result recovery, and restart recovery.
- Hub availability evaluation: passed.
- MCP terminal-reconciliation evaluation: passed; final report survived and
  wrapper cleanup followed authoritative `session_task_complete`.

## Deployment Invariants

- Preserve Hub SQLite state and Edge job state, Codex homes, worktrees, and repo
  checkouts.
- Do not restart an Edge while a real Codex turn is active.
- Deploy one Edge at a time so fleet capacity remains available.
- Run containerized Edges with an init/reaper; do not change unrelated host
  services or mounts.
- Keep the public MCP URL unchanged.
- After rollout, run the four startup calls through the real public connector,
  continue an existing worker, create a disposable group, and verify cross-group
  isolation before declaring production ready.
