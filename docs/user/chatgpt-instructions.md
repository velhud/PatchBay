# ChatGPT client instructions

PatchBay exposes two different workflows. Determine which one the user selected before acting.

## Manager/worker mode

In manager/worker mode, ChatGPT Pro manages local Codex colleagues. Workers perform non-trivial repository investigation, implementation, execution, tests, and detailed review. ChatGPT briefs them, compares reports, asks follow-up questions, decides acceptance, and synthesizes the result.

Direct read/search/git tools remain available for orientation, worker briefing, focused verification, contradictions, risky or failed changes, tiny tasks, and explicit user requests. Do not turn them into the default broad implementation loop.

### Single-machine startup

1. Call `codex_self_test`.
2. Call `codex_open_workspace`, or use `codex_list_workspaces(query=..., discover=true)` when the exact path is unknown.
3. Call `codex_worker_options` for the installed catalog state.
4. For ordinary workers, explicitly pass `model="gpt-6-luna"` and `reasoning_effort="high"` when supported. Use the exact returned Luna identifier if it differs.
5. Report any fallback. Never silently switch models or fabricate support.
6. Start named workers with complete colleague-quality briefs.
7. Continue the same worker with `codex_worker_message` when correction or deeper evidence is needed.
8. Use `codex_worker_wait`, status, list, and inspect at the returned cadence. Quiet work is normal.
9. Inspect reports before raw files or diffs. Preview isolated integration before applying it.
10. Stop only deliberately. Integration does not commit and does not delete the worktree.

Terra and Sol are explicit escalations. Spark, GPT-5.4 Mini, GPT-5.4, and GPT-5.5 are explicit availability, compatibility, or evidence-backed fallbacks. The previous Spark-first/automatic-Mini rule is obsolete.

### Hub/Edge startup

When the connector exposes `patchbay_*` tools, use the Hub workflow rather than looking for single-machine `codex_*` tools.

1. Inspect fleet, workspaces, and existing groups.
2. Resume the exact existing group or create one durable group for the user task.
3. Wait for Edge preflight.
4. Start one worker or a batch of named lanes. A group remains pinned to one machine unless explicitly reassigned.
5. Use group-scoped worker status/wait/inspect and natural-language follow-up.
6. Use `patchbay_operation_status` for the exact operation when recovery is needed. Never expose internal reconciliation transitions.
7. Close only after active or uncertain operations are terminal and every worker has a truthful disposition.

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

The normal lifecycle is:

```text
register repository -> create task -> claim lease -> export context ->
write package -> apply revision -> run checks -> inspect feedback ->
approve with actor -> publish
```

Author execution qualification is Linux/WSL2-only. Do not ask PatchBay to weaken the guard or reinterpret a guarded macOS run as multiple independent source defects.

## Brief and evidence quality

For manager workers, include purpose, current state, outcome, scope, constraints, non-goals, lane relationships, deliverables, and required evidence. Let workers locate relevant files unless exact paths are already useful.

For Author packages, include only declared deterministic content. Keep package paths relative and manifest-bound. Do not encode instructions to launch models, mutate unrelated repositories, or escape the candidate root.

In both modes, report exact verification and distinguish checks actually run from checks still required.
