# End-To-End Algorithms

Status: Phase 4 worker algorithms implemented; app-server backend pending.

## Start Worker

```text
Input: human name, natural-language brief, optional repo, optional workspace_mode

1. Resolve and validate the authorized repository.
2. Reject a duplicate worker name case-insensitively within the same base repository. The same display name may be reused in another workspace.
3. Generate a stable private worker id.
4. Resolve `workspace_mode`, defaulting to `isolated_write`.
5. For `isolated_write`, create one external git worktree and private branch.
6. Create an existing durable `interactive` job with private worker identity and workspace options.
7. Compose report guidance plus the natural-language brief.
8. Start Codex through the existing job executor.
9. Return the public worker pointer without exposing backend internals.
```

Failure behavior:

- preserve the worker-tagged durable job with a concise error;
- do not silently create a worker if repository validation fails.

## Derive Worker View

The current implementation does not add a separate worker record. The public worker view is derived from existing durable jobs:

```text
1. Group durable jobs by private `_worker_id`.
2. Reconcile stale durable `running` jobs that no longer have a tracked Codex subprocess after the launch grace window.
3. Sort each worker's jobs by active state and activity timestamp.
4. Map latest pending/running job to `starting` or `working`.
5. Map completed job to `idle` and build the latest report from result summary/notes/next steps.
6. Map failed/cancelled job to `failed` or `stopped` with redacted public text.
7. Derive session availability from the latest stored Codex session reference.
8. Derive workspace availability and change presence without exposing private paths.
9. Return only public worker fields.
```

## Resolve Worker By Name

```text
Input: worker name or worker id, optional repo_path

1. If the value is a worker id, resolve it directly.
2. If the value is a human name, match it case-insensitively inside the requested/current base repository.
3. If multiple same-name workers remain possible, return a bounded ambiguity error and ask for repo_path or worker_id.
4. Do not let an old worker from another repository block worker creation or lookup in the current repository.
```

No event bus, scheduler, database, or startup reconciliation loop is required. The executor-level reconciliation is a lightweight status repair pass, not a worker queue.

## Message Worker

```text
Input: worker name/id, natural-language message

1. Resolve worker and reconcile it.
2. If the latest turn is pending/running, return `accepted: false`; the wrapper does not queue or steer.
3. If target worker is idle with a session id, create a durable `resume` job with that session id.
4. Reassert the worker workspace and sandbox before the Codex `resume` subcommand.
5. If an isolated worker worktree is missing or discarded, return `accepted: false` and do not fall back to the base checkout.
6. If target worker is idle without a session id, return `accepted: false` and tell ChatGPT to inspect/start a new worker.
7. Return accepted/rejected public worker view.
```

The caller never supplies session ID or worktree path.

## List Workers

```text
1. Load workers, optionally scoped by repository.
2. Reconcile each worker.
3. Calculate lightweight derived fields:
   - session availability;
   - message availability;
   - latest report.
4. Return bounded public summaries ordered by recent activity.
```

ChatGPT can translate the result into a natural-language team update.

## Inspect Worker

Report/status views return persisted latest report plus current execution state.

Changes view reads git state from the worker workspace and includes untracked files in the inventory.

Diff view returns a bounded one-file diff for a workspace-relative path. It rejects absolute or escaping paths and omits private workspace paths.

Output view remains deferred. If added later, it must return bounded and redacted latest-job output without exposing raw prompts or unrestricted transcripts.

Integration preview runs the read-only preview algorithm below.

## Stop Worker

```text
1. Resolve worker.
2. If the latest job is active, cancel through existing execution controls.
3. Preserve prior worker-tagged jobs and session continuity.
4. If `cleanup_workspace=true`, remove the isolated worker worktree and private branch.
5. Mark the worker workspace discarded so future messages refuse to continue instead of writing in the base checkout.
6. Return whether an active turn was stopped and whether cleanup happened.
```

## Worker From Another Worker

Deferred. Phase 1 has no `from_worker` shortcut.

Exec backend V1:

```text
1. Read source worker latest report and selected metadata.
2. Create a new independent worker/worktree.
3. Include concise attributed handoff in the new brief.
4. Start a new Codex session.
```

App-server backend V2 may use a real thread fork after it is verified.

## Integration Preview

Implemented in Phase 4:

```text
1. Reconcile worker and ensure no active write is running.
2. Verify worker worktree exists and belongs to the base repository.
3. Record worker base revision.
4. Build a complete patch from worker worktree.
5. Inspect target workspace status.
6. If target has unsafe uncommitted changes, do not modify it.
7. Run `git apply --check` against the base checkout.
8. Report clean files/stats or conflicts.
```

The preview must be read-only with respect to the user's target workspace.

## Integrate Worker

Implemented in Phase 4:

```text
1. Run integration preview and require a clean result.
2. Require target working tree cleanliness unless a later explicit strategy supports dirty targets.
3. Rebuild the exact patch.
4. Apply the patch to the target workspace.
5. Verify applied file inventory matches preview.
6. Return success and changed files.
7. Do not commit automatically in V1.
8. Preserve the worker worktree by default.
```

If any step fails, stop and report. Do not partially copy files manually.
