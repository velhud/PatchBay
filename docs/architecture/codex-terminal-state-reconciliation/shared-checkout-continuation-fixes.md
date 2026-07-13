# Shared-Checkout Continuation Follow-Up

Status: `RELEASE_CANDIDATE_VERIFIED_DEPLOYMENT_PENDING`

## Verified Runtime Facts

The private continuation report was accurate with one important distinction:
the writer lock and stale integration-token rejection were deliberate safety
behavior, while preflight currentness was a real stale-state defect. Deployment
identifiers and repository-specific revisions remain in ignored private evidence.

- Three parallel workers were requested as `shared_write`. One acquired the
  base-checkout mutation lock; two were safely refused with `repo_busy`.
- Two Golden Acceptance integration attempts used preview tokens whose bindings
  no longer matched the changed base checkout. Both were safely rejected as
  `stale_preview_token`; no worker changes were deleted or overwritten.
- Group readiness still claimed `currentness: current` and a clean checkout
  after an accepted worker integration had made the base checkout dirty. That
  claim was stale and required a runtime fix.
- The worker-local environment could run tests through its interpreter. A
  missing standalone `pytest` command did not prove pytest was unavailable.

## Selected Fixes

1. Preserve serialized base-checkout locking as the default policy.
2. Superseding policy note: this incident originally required rejecting every
   batch containing multiple `shared_write` workers. Current PatchBay keeps
   that serialized default but allows an architect to choose
   `shared_write_policy=manager_controlled` for deliberate concurrent base
   writers with explicit ownership boundaries. `isolated_write` remains the
   recommended parallel implementation mode; concurrency is an architect
   decision rather than an unoverrideable runtime restriction.
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
- Full suite: `955 passed, 4 skipped` on macOS and `958 passed, 1 skipped`
  in the production Linux environment. Both runs cover the same 959-test
  inventory with platform-specific skips; only existing framework/cache
  warnings remain.
- Public MCP terminal-reconciliation eval: passed with 31 tools.
- Hub availability/routing eval: passed.
- Consequential Hub V2 eval: passed, including integration, durable result
  recovery, Hub/Edge restart, same-worker session/workspace continuation, and
  explicit preflight reconciliation after base integration.

## Rollout Plan

Deployment is authorized only after the complete release gate below passes:

1. Recheck the public diff for private data and credentials.
2. Commit and push the verified change set.
3. Require GitHub CI and CodeQL success.
4. Deploy Hub first without changing the public URL.
5. Deploy production Edges sequentially, preserving state and active worker
   safety.
6. Repeat the public 31-tool check and one disposable integration scenario.
7. Verify the resulting group reports `currentness: refresh_required`, then
   resume it and verify strict preflight returns current HEAD and dirty state.
