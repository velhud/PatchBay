# Phase 4 — Worker Integration Preview And Accepted-Result Application

Status: implemented and locally verified.

## Purpose

Phase 4 adds the missing human management act:

```text
Use this worker's result.
```

The wrapper now lets ChatGPT preview whether an isolated writing worker's changes can be applied to the base checkout, then apply the accepted result explicitly.

This is not a merge queue, PR system, reviewer bureaucracy, branch-management platform, or automatic promotion engine. It is the smallest reliable bridge between a worker's private desk and the main checkout.

## Public surface

The worker surface remains small.

### `codex_worker_inspect(view="integration_preview")`

Read-only. It answers:

- whether the worker is idle;
- whether the worker is an isolated writing worker;
- whether the worker has changes;
- whether the base checkout is dirty;
- whether the base branch moved since the worker started;
- whether blocked or secret-like paths are present;
- whether the worker patch applies cleanly;
- if not clean, the bounded conflict summary.

It does not mutate the base checkout.

### `codex_worker_integrate`

Mutating. It applies one accepted isolated writing worker result to the base checkout using git patch mechanics.

It does not:

- commit changes;
- delete the worker worktree;
- merge every worker;
- resolve conflicts automatically;
- expose job ids, Codex session ids, private paths, branch names, raw transcripts, or raw logs.

The normal result is:

```text
Worker result applied to the base checkout. Review, test, and commit from the normal repository workflow. The worker worktree was preserved.
```

## Behavior

The integration flow is:

```text
worker-owned worktree
  -> changed-file inventory
  -> blocked-path check
  -> patch construction
  -> git apply --check against base checkout
  -> explicit apply only when requested
```

Preview and apply use the same patch construction path. Tracked changes are collected through `git diff --binary HEAD`. Untracked text files are represented as new-file patches. Unreadable or binary untracked files are not silently copied; preview reports that manual integration is required.

## Dirty base policy

By default, integration refuses a dirty base checkout.

This is not micromanagement. It is an exact boundary where a worker's private result crosses into the user's main source tree. A dirty base makes it impossible to tell whether conflicts belong to the worker result or unrelated local work.

An expert override exists:

```json
{
  "worker": "Implementer",
  "allow_dirty_base": true
}
```

Even with this override, `git apply` still has to succeed.

## Conflict behavior

If the patch does not apply cleanly, Phase 4 reports the conflict in ordinary language plus bounded git output. It does not mutate the base checkout.

The expected next move is natural:

```text
Ask the implementer to revise against current main.
```

or:

```text
Ask another worker to inspect the conflict and propose the smallest manual resolution.
```

## Preserved boundaries

Phase 4 intentionally does not add:

- automatic conflict resolution;
- automatic commits;
- cleanup-after-apply;
- worker ranking;
- mandatory reviewer flow;
- full branch merge/cherry-pick workflow;
- app-server backend migration.

Those can be separate later phases if real usage proves they are needed.

## Acceptance scenario

```text
1. Start a writing worker.
2. Let it produce changes in its isolated worktree.
3. Inspect its report.
4. Preview integration.
5. Apply the accepted result.
6. Confirm the base checkout now has the changes.
7. Confirm the worker worktree still exists.
8. Run normal tests and commit manually.
```
