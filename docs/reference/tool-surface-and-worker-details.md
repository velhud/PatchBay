# Tool surface and worker details

This document records operational details that apply to the current unified implementation. The authoritative schemas remain in source.

## Runtime composition

The effective single-machine worker runtime is `runtime.WorkerRuntime` composed with the critical integration and view fixes. The older audit branch's `ReliableWorkerRuntime`, `GitCandidateMixin`, and `ReadSafetyMixin` are not activated because they would override later complete candidate and pagination behavior.

Public wrapper modules may delegate to adjacent `*_legacy.py` implementation modules. Those files are compatibility layers, not dead code.

## Candidate and integration behavior

- Git candidates are constructed from complete machine-safe path data.
- Delivered snapshots establish the integration baseline.
- Acceptance becomes stale when the candidate changes.
- Integration preview is explicit and token-bound.
- Application preserves all non-delivered content and does not commit.
- Cleanup requires complete delivery or explicit discard proof.
- Read-only workers cannot mutate through Git options or command paths.

Optional local start/message retry keys are persisted in the existing atomic
job options, serialized through a process-released admission lock. Replaying
the exact owner/method/key/arguments reads the original admission, including
after restart; it does not schedule inference. No key, a new key or deliberate
`auto_suffix` remains a separate request. Unsupported active-turn follow-ups
are refused rather than enqueued. Keyed integration uses the existing signed
preview/receipt mechanism and requires both token and key.

## File and diff views

Worker file views are lossless over contiguous source lines. When a line or response reaches the byte budget, pagination stops rather than skipping ahead and inventing line numbers. Responses include an exact continuation cursor and report when a requested `max_bytes` value was capped by server policy.

Bounded output is a transport-stability contract. Managers may request the next page or a focused file/diff view when additional evidence is required.

Compact Manager text keeps independent outcome, candidate/source, partial-page,
cleanup and recovery fields when returned. It does not replace exact file/log
bodies. Oversized scalar identities/cursors are explicitly marked omitted and
remain intact in structured output. See the [MCP interface contract](mcp-interface-contract.md).

## Process and concurrency behavior

- Repository mutation uses cross-process serialization.
- Process ownership includes the complete tracked process tree.
- Signal delivery requires proven identity; uncertain or recycled identities fail closed.
- Terminal semantic evidence is persisted before bounded wrapper cleanup.
- Cancellation, completion, and unknown execution cannot silently upgrade one another after a terminal decision.
- Hub/Edge attempt, lease, receipt, projection, and idempotency state remains durable and monotonic.

A delayed Hub resume rechecks current controller ownership before its final
group write. Exact fenced late receipts may repair the existing narrow
absence-based manual-recovery blocker; ordinary stale results cannot overwrite
terminal decisions. See the [S02 identity, crash and recovery map](../repairs/2026-10-22-s02-controller-recovery.md).

## Manager polling and reports

Status/list/wait/compact-inspect are bounded coordination views. Poll at the returned cadence, normally 20–30 seconds. `poll_too_early`, cached status, or a wait timeout is not a worker failure.

Completed reports should include summary, detailed report, evidence, changed files, commands/tests, notes, risks, open questions, and next steps when Codex emitted them. Diagnostics and raw lifecycle details are explicit debugging views, not the default manager payload.

## Model and reasoning selection

Every delegated worker MUST explicitly use model='gpt-6-luna' with reasoning_effort='high' by default; xhigh or max is allowed. No other model family, older Luna, ambient default, fallback, or escalation is permitted. If Luna is unavailable, report the blocker. Apply this same rule to nested helpers and every handoff. The primary manager is separate from delegated workers; Author Mode has no delegated author or recovery worker.

Every delegated worker MUST explicitly use model='gpt-6-luna' with reasoning_effort='high' by default; xhigh or max is allowed. No other model family, older Luna, ambient default, fallback, or escalation is permitted. If Luna is unavailable, report the blocker. Apply this same rule to nested helpers and every handoff. The primary manager is separate from delegated workers; Author Mode has no delegated author or recovery worker.

## Author Mode details

Author Mode has its own state home, leases, tasks, revisions, operations, approvals, publications, tokens, and qualification records. It shares no worker initialization path.

Selected reconciliation safeguards:

- bounded CLI waits (1–86,400 seconds; flag-only default 3,600);
- symlink rejection for local lease caches;
- required approval actor and token role;
- HTTP access logging disabled by default;
- private instance ID removed from public qualification output;
- remote package-path validation, maintenance locking, lease checks, unknown-execution fencing, supervisor-launch error handling, JSON-RPC validation, task/publication operations, safe exports, and honest CLI failure codes retained.

The Linux/WSL2 guard remains part of the execution qualification contract.

## Compatibility and exclusions

Export-only branches and temporary delivery workflow material are not production behavior and are not imported merely to merge every branch name. Separate dependency maintenance and external Desktop bridge work remain separate until independently qualified.
