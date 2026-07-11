# Implementation And Verification Plan

## Phase 0: Runtime Safety Gate

1. Confirm no active production operation would be interrupted.
2. Record current PatchBay and Codex versions on each Edge.
3. Preserve private incident evidence outside the public repository.
4. Reproduce the mismatch with a local fake Codex wrapper before editing.

## Phase 1: Session Parser

1. Add exact-session resolution and incremental JSONL observation.
2. Add fixtures for valid, partial, malformed, unrelated, and appended records.
3. Prove that only strict terminal events complete a session.

## Phase 2: Executor Integration

1. Start observation only after the exact session ID exists.
2. Normalize stdout and session terminal evidence.
3. Capture final result before post-completion cleanup.
4. Add bounded wrapper cleanup without a task-duration limit.
5. Ensure stdout/stderr reader tasks cannot deadlock after wrapper termination.

## Phase 3: Terminal State Arbitration

1. Add atomic first-terminal-transition semantics to JobManager.
2. Route executor completion, failure, cancellation, shutdown, and stale
   reconciliation through the same boundary.
3. Persist additive terminal and cleanup diagnostics.

## Phase 4: Restart Reconciliation

1. Recover exact-session terminal evidence before declaring tracking loss.
2. Recover a bounded report when possible.
3. Preserve cancellation precedence and late-evidence diagnostics.

## Phase 5: Unit And Integration Tests

Required scenarios:

1. session `task_complete` plus final message while wrapper never exits;
2. intermediate agent message without terminal event remains running;
3. unrelated recent session is ignored;
4. partial JSONL line is retried, not misclassified;
5. malformed records do not fail the worker;
6. duplicate terminal events are idempotent;
7. normal process exit follows the existing path;
8. unavailable session log follows the existing path;
9. cancellation before terminal remains cancelled;
10. terminal committed before cancellation remains completed;
11. concurrent cancellation/terminal race has deterministic first-writer
    behavior;
12. lingering wrapper is terminated only after terminal evidence and grace;
13. write-worker locks and integration readiness are not released early;
14. restart reconciliation recovers a terminal session;
15. restart reconciliation never adopts another session;
16. final result is redacted and manager-readable;
17. Hub group projection becomes completed without Hub inference.

## Phase 6: Standard Verification

Run:

```bash
python -m compileall src scripts tests
python -m pytest tests -q
python scripts/live_mcp_eval.py --json
python scripts/live_hub_edge_eval.py --json
python scripts/live_hub_v2_eval.py --json
```

Record `codex --version`. Unexpected lockfile or manifest changes fail the gate.

## Phase 7: Outside-In Live Tests

Use the actual public MCP surface, not internal function calls:

1. start a disposable Hub group on a temporary repository;
2. run several cheap read-only workers in parallel;
3. include a controlled wrapper-linger shim that writes real-shaped session
   `task_complete` but does not exit;
4. poll at normal manager cadence, not continuously;
5. verify workers become completed, reports are inspectable, follow-up turns
   work, and the group closes;
6. run a write-worker case and confirm worktree safety and integration state;
7. restart an Edge between terminal recording and Hub acknowledgement, then
   verify durable recovery;
8. verify public outputs contain no raw prompts, session paths, or transcript.

## Phase 8: Rollout

1. Commit and push only after privacy review and all local gates pass.
2. Build and deploy Edges one at a time so fleet capacity remains available.
3. Keep the existing Hub URL and manager tool manifest unchanged.
4. Deploy the Hub package only if required by the shared release, without
   changing its public contract.
5. Repeat one outside-in multi-Edge group run.
6. Keep the previous image/commit ready for rollback.

## Acceptance Criteria

- A worker with exact-session `task_complete` cannot remain running solely
  because its wrapper lingers.
- No legitimate non-terminal worker is completed from silence or message text.
- No arbitrary maximum work duration is introduced.
- Cancellation and completion races are deterministic and durable.
- Manager-facing reports survive wrapper cleanup and Edge restart.
- Hub group state reflects corrected Edge truth without new Hub heuristics.

## Local Verification Evidence

Completed before commit or deployment:

- `python -m compileall src scripts tests`: passed.
- `python -m pytest tests -q`: 645 passed after the cross-project reliability refinements.
- `python scripts/live_mcp_eval.py --json`: passed with 31 tools.
- `python scripts/live_mcp_eval.py --json --exercise-terminal-reconciliation`:
  passed through public MCP worker start and inspect; the final report survived,
  terminal source was `session_task_complete`, and the lingering wrapper was
  terminated after completion.
- `python scripts/live_hub_edge_eval.py --json`: passed.
- `python scripts/live_hub_v2_eval.py --json`: passed, including 31-tool Hub
  surface, group pinning, worker continuation, integration, and restart
  recovery.
- Codex CLI recorded during verification: `0.144.1`.

The release also verifies zombie PID detection, terminal-report recovery before
manager cancellation, exact child-repository binding, live Edge workspace
discovery, group-scoped aggregate monitoring, nullable Hub projection fields,
distinct workspace-instance identity, identifier-rich MCP text fallback, and
automatic projection-time reconciliation so lost workers cannot consume router
capacity indefinitely.

No release, push, service restart, or machine deployment was performed.
