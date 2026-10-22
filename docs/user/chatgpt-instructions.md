# ChatGPT client instructions

PatchBay exposes two different workflows. Determine which one the user selected before acting.

## Manager/worker mode

In manager/worker mode, ChatGPT Pro manages local Codex colleagues. Workers perform non-trivial repository investigation, implementation, execution, tests, and detailed review. ChatGPT briefs them, compares reports, asks follow-up questions, decides acceptance, and synthesizes the result.

Direct read/search/git tools remain available for orientation, worker briefing, focused verification, contradictions, risky or failed changes, tiny tasks, and explicit user requests. Do not turn them into the default broad implementation loop.

### Single-machine startup

1. Call `codex_self_test`.
2. Call `codex_open_workspace`, or use `codex_list_workspaces(query=..., discover=true)` when the exact path is unknown.
3. Call `codex_worker_options` for the installed catalog state.
4. For ordinary workers, explicitly pass `model="gpt-6-luna"` and `reasoning_effort="high"` (`xhigh` or `max` are also supported). Check availability first; an unavailable Luna is a blocker.
5. Start named workers with complete colleague-quality briefs.
6. Continue the same worker with `codex_worker_message` when correction or deeper evidence is needed.
7. Use `codex_worker_wait`, status, list, and inspect at the returned cadence. Quiet work is normal.
8. Inspect reports before raw files or diffs. Preview isolated integration before applying it.
9. Stop only deliberately. Integration does not commit and does not delete the worktree.

Every delegated worker MUST explicitly use model='gpt-6-luna' with reasoning_effort='high' by default; xhigh or max is allowed. No other model family, older Luna, ambient default, fallback, or escalation is permitted. If Luna is unavailable, report the blocker. Apply this same rule to nested helpers and every handoff. The primary manager is separate from delegated workers; Author Mode has no delegated author or recovery worker.

Give each deliberate local start/follow-up a stable `idempotency_key` when
retry recovery is needed. After a lost or invalid response, inspect existing
work first, then keep the exact key and arguments. A new key or `auto_suffix`
can create independent work. Unkeyed calls have no replay guarantee. Active
turns do not queue messages. Supply both signed preview token and key for
keyed integration. Do not change owner scope or take over merely to retry.

### Hub/Edge startup

When the connector exposes `patchbay_*` tools, use the Hub workflow rather than looking for single-machine `codex_*` tools.

The complete canonical Hub export has 31 tools. A scoped trial, permission view
or currently loaded subset is a different observation. Diagnose unavailable
actions using the selected plugin/profile, permissions and supported complete
contract capture; do not infer stale metadata from a visible subset alone.

1. Inspect fleet, workspaces, and existing groups.
2. Resume the exact existing group or create one durable group for the user task.
3. Wait for Edge preflight.
4. Start one worker or a batch of named lanes. A group remains pinned to one machine unless explicitly reassigned.
5. Use group-scoped worker status/wait/inspect and natural-language follow-up.
6. Use `patchbay_operation_status` for the exact operation when recovery is needed. Never expose internal reconciliation transitions.
7. Close only after active or uncertain operations are terminal and every worker has a truthful disposition.

When `patchbay_work_group_reassign` returns `handoff_context` with
`context_mode="reports_and_changes"`, treat its bounded report list as an
index. It does not transfer files or diffs, even if the requested mode includes
`reports_and_changes`. Follow `required_changes_retrieval.actions` using the
exact predecessor group ID and `fleet_worker_ref`: enumerate every worker by
following `next_cursor`, read each needed report and changed-file inventory,
then retrieve file contents through `view="file"` pages and verify the stable
per-file SHA-256. Route each read directly to the predecessor ID; do not try to
make a superseded group current again. Diff output may be truncated. Read predecessor content before
giving selected material to the successor, and inspect the successor's own
candidate and checks. If the predecessor route is unavailable or a response
cannot be completed, report the retrieval blocker and do not claim that the
content moved.

A queued operation is accepted durable work, not proof of completed Edge execution. A wait timeout is not permission to answer as though the task finished.

### Shared-server behavior

One Server URL shares local state across connected clients. Reads may be shared. Cross-owner mutation requires explicit `takeover: true` after user confirmation. Under serialized policy, base-checkout contention should return `repo_busy`; do not bypass the lock.

## Author Mode

In Author Mode, ChatGPT Pro writes the actual implementation. PatchBay is a deterministic transport and qualification system. It validates the package, materializes an isolated candidate, runs authorized checks, and returns evidence.

Do not call worker tools. Do not initialize Codex. Do not delegate implementation or failure recovery to another model. A failed check means inspect Author feedback/logs/diff, correct the source package, and submit a new revision.

Use the independent Author entrypoints:

```text
patchbay author ...
patchbay-author ...
```

The writable/operator lifecycle is:

```text
register repository -> create task -> claim lease -> export context ->
write package -> apply revision -> run checks -> inspect feedback ->
approve with actor -> publish
```

A read-only connection recovers existing tasks and reads pinned source,
feedback, logs and operations. Use an existing artifact if available; creating
a context or feedback export requires an authorized writer/operator. Do not
invoke hidden export tools or change modes to bypass the role.

For `author_read_file`, select `context_id` alone or `draft_id` plus exact
`draft_version`. Recover IDs and draft versions from the existing task rather
than guessing. Bounded field errors describe repairable argument constraints.
After a post-dispatch output/size fault, no success or absence of an effect is
asserted. Follow the returned read-only recovery actions before another
mutation; an identical retry keeps its original key and payload.

For complete Author history during activity, use `traversal="snapshot"` on
`author_recent_operations`, carrying its cursor and next offset through every
page. It pins operation identities, not authority or a single historical state;
recover current task/revision/lease again before acting. Strict traversal remains
the default and rejects same-task changes between pages.

Author execution qualification is Linux/WSL2-only. Do not ask PatchBay to weaken the guard or reinterpret a guarded macOS run as multiple independent source defects.

## Brief and evidence quality

For manager workers, include purpose, current state, outcome, scope, constraints, non-goals, lane relationships, deliverables, and required evidence. Let workers locate relevant files unless exact paths are already useful.

For Author packages, include only declared deterministic content. Keep package paths relative and manifest-bound. Do not encode instructions to launch models, mutate unrelated repositories, or escape the candidate root.

In both modes, report exact verification and distinguish checks actually run from checks still required.

See [heavy-task setup and copy/paste prompts](chatgpt-heavy-tasks.md) for choosing
between direct Author coding and worker delegation. Read these instructions once
when orienting a conversation; routine status calls do not need the whole guide
pasted back into the task.
