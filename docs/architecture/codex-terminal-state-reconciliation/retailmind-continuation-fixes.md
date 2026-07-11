# RetailMind Continuation Follow-Up

Status: `IMPLEMENTED_LOCALLY_VERIFIED_NOT_DEPLOYED`

## Verified Runtime Facts

The RetailMind continuation report was accurate with one important distinction:
the writer lock and stale integration-token rejection were deliberate safety
behavior, while preflight currentness was a real stale-state defect.

- Three parallel workers were requested as `shared_write`. One acquired the
  base-checkout mutation lock; two were safely refused with `repo_busy`.
- Two Golden Acceptance integration attempts used preview tokens whose bindings
  no longer matched the changed base checkout. Both were safely rejected as
  `stale_preview_token`; no worker changes were deleted or overwritten.
- Group readiness still claimed `currentness: current`, clean checkout, and HEAD
  `4cfeb2d` after an accepted worker integration had made the base checkout
  dirty. That claim was stale and required a runtime fix.
- The worker-local environment could run tests through its interpreter. A
  missing standalone `pytest` command did not prove pytest was unavailable.

## Selected Fixes

1. Preserve one-writer base-checkout locking.
2. Reject batches containing multiple `shared_write` workers before creating
   any child operation. Parallel implementers must use `isolated_write`; direct
   base writers run sequentially.
3. Preserve strict integration-token binding. A stale-token response now states
   that it is retryable and directs the manager to request a fresh
   `integration_preview`.
4. Mark group preflight `currentness: refresh_required` after accepted worker
   integration or accepted shared-write start. Historical facts stay visible,
   but are no longer presented as live git truth. The group remains operational;
   resume performs strict refresh when current facts are needed.
5. Tell managers to try repository instructions and `python -m pytest` or
   `python3 -m pytest` before declaring the test dependency unavailable.

## Verification

- Focused Hub adapter/runtime/integration tests: passed.
- Full suite: 636 passed; four existing FastAPI lifespan deprecation warnings.
- Public MCP terminal-reconciliation eval: passed with 31 tools.
- Hub availability/routing eval: passed.
- Consequential Hub V2 eval: passed, including integration, durable result
  recovery, restart recovery, and explicit preflight invalidation after base
  integration.

## Rollout Plan

No deployment is part of this pass.

When deployment is authorized:

1. Recheck the public diff for private data and credentials.
2. Commit and push the verified change set.
3. Require GitHub CI and CodeQL success.
4. Deploy Hub first without changing the public URL.
5. Deploy production Edges sequentially, preserving state and active worker
   safety.
6. Repeat the public 31-tool check and one disposable integration scenario.
7. Verify the resulting group reports `currentness: refresh_required`, then
   resume it and verify strict preflight returns current HEAD and dirty state.
