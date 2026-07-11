# Natural-Language Worker Bridge

Status: durable named workers, isolated worker worktrees, peer-worker context, artifact inbox transfer, explicit integration, and shared-server multi-client coordination are implemented. Direct MCP and tokenized public-tunnel artifact-flow validation evidence exists. The active internal ChatGPT Pro to private VM worker loop is working reliably for current self-use. Formal multi-browser ChatGPT Developer Mode validation remains pending, especially multiple independent ChatGPT conversations sharing one Server URL. App-server backend work remains deferred.

This directory defines the worker layer for `patchbay`.

The current application exposes a local Streamable HTTP MCP bridge that lets ChatGPT inspect configured repositories, launch local Codex jobs, and manage named Codex workers. The worker layer makes the normal product abstraction human: ChatGPT briefs named local Codex colleagues, continues them by name, imports generated files or zips as artifact context, inspects reports and diffs, passes bounded context between workers, previews integration, and explicitly applies accepted work through exact git mechanics.

The intended ChatGPT posture is active management, not one-shot delegation and not direct manual repository reading. For non-trivial work, ChatGPT should first decide which worker or worker team to appoint. Named workers are continuing specialists. For important work, ChatGPT should ask workers for durable report files or changed-file evidence, inspect results, then use `codex_worker_message` for follow-up questions when reports are thin, contradictory, missing validation, or need another worker's context. Direct read/search tools remain available for orientation, focused verification, exact line/diff checks, and tiny exceptions, but broad investigation and implementation should flow through workers.

Hub work groups default to serialized base-checkout writes. An architect may
explicitly choose `shared_write_policy=manager_controlled` to permit concurrent
shared-write workers in one checkout; PatchBay exposes that policy and leaves
ownership boundaries and conflict reconciliation to the manager. Stale
integration attempts return a fresh preview/token for review, completed base
mutations reconcile current Git facts, and Edge preflight reports detected
repository-local test environments without requiring one.

## Read Order

1. [00 Overview](00_ARCHITECTURAL_OVERVIEW.md)
2. [01 Current State And Gaps](01_CURRENT_STATE_AND_GAPS.md)
3. [02 Target Architecture](02_TARGET_ARCHITECTURE.md)
4. [03 Public MCP Contract](03_PUBLIC_MCP_CONTRACT.md)
5. [04 Runtime State Schema](04_RUNTIME_STATE_SCHEMA.md)
6. [05 End-To-End Algorithms](05_END_TO_END_ALGORITHMS.md)
7. [06 Implementation History](06_IMPLEMENTATION_PHASES.md)
8. [07 Repository Change Map](07_REPOSITORY_CHANGE_MAP.md)
9. [08 Testing And Release](08_TESTING_AND_RELEASE.md)
10. [09 Historical Package Protocol](09_PHASE_PACKAGE_PROTOCOL.md)
11. [10 Decisions Risks And Deferred Work](10_DECISIONS_RISKS_AND_DEFERRED.md)
12. [Durable Workers Implementation Note](PHASE1_DURABLE_WORKERS.md)
13. [Writing Workers Implementation Note](PHASE2_WRITING_WORKERS.md)
14. [Multi-Worker Coordination Implementation Note](PHASE3_MULTI_WORKER_COORDINATION.md)
15. [Integration Implementation Note](PHASE4_INTEGRATION.md)
16. [Multi-Chat Concurrency Plan](MULTI_CHAT_CONCURRENCY_PLAN.md)

## Implementation Status

The current worker surface stays small while covering setup options, artifact transfer, lifecycle, inspection, and explicit integration:

- `codex_worker_options`;
- `codex_worker_inbox`;
- `codex_worker_start`;
- `codex_worker_message`;
- `codex_worker_list`;
- `codex_worker_status`;
- `codex_worker_wait`;
- `codex_worker_inspect`;
- `codex_worker_integrate`;
- `codex_worker_stop`.

The implementation derives worker identity and reports from existing durable job records and Codex session references. Worker display names are scoped to the base workspace, so the same human name can exist in separate repos; `auto_suffix` handles repeated phase reruns in one workspace. Default workers use one external isolated worktree across turns and PatchBay restarts. Worker change, paged file, and diff inspection is available on demand; before integration, use `codex_worker_inspect(view="file", file_path="...")` for worker-created file content because `codex_read_file` reads the base checkout. Large file views return `next_start_line` for pagination, and worker-created report files are labeled as worker-worktree-only until explicitly integrated or copied. `codex_worker_options` provides a bounded model/reasoning menu from the installed Codex runtime/catalog and accepts `repo_path` as an ignored compatibility field; `codex_worker_inbox` imports ChatGPT-generated files/zips into local runtime storage and returns artifact ids; `codex_worker_start` and `codex_worker_message` can then set or inherit `model` and `reasoning_effort` while also including bounded `report`, `changes`, `diff`, or `review` context from other workers and selected artifacts. `review` includes report plus changed-file inventory plus bounded diff for pre-integration review. Imported artifacts are copied into `.ai-bridge/imported-artifacts/` inside isolated worker worktrees and excluded from integration, while `include_untracked_from_base` can copy selected accepted untracked base files into a new isolated worker. Unchanged copied base-context files are excluded from integration patches; modified copied base-context files block automatic apply through `modified_included_untracked_base_files`. `codex_worker_status` returns the compact pull-based team status bar with active/quiet/stale/lost counts, deltas since the last status check, one short line per worker, and recommended polling cadence; compact status omits raw shell command text, `codex_worker_inspect(view="status")` gives one-worker liveness diagnostics, and `view="diagnostics"` exposes full lifecycle internals only for deliberate debugging. `view="report"` is the normal worker-answer view and avoids low-level `latest_turn` detail. Normal ChatGPT monitoring should wait about 10-20 seconds between status calls instead of polling every few seconds. Too-early status calls return a cached `poll_too_early` response without resetting activity deltas, and `codex_worker_wait` waits once before returning fresh compact status. `codex_worker_stop` can return `stop_confirmation_required` for live or recently started workers; ChatGPT should wait or retry with `force=true` only after a deliberate cancellation decision. `codex_worker_list` returns the same `team_status` plus a compact `team_report` and supports filters for active, non-stopped, current-owner, or recently created workers. PatchBay streams Codex JSON events and records the session as soon as `thread.started` appears; a startup-only timeout can fail a process that never creates a session without imposing an overall limit on long Codex turns. Streaming updates maintain heartbeat, event/output counters, phase, command preview in diagnostics, and latest partial note/checkpoints for the status layer. Terminal jobs clear live-only command fields, and result artifacts are persisted even when Codex only produced a latest agent message or bounded raw-output fallback instead of a final structured result event; artifact metadata exposes the result source and whether a final Codex result event, turn completion, and parsed schema were seen. Persisted running jobs reload as recovered-running records and are reconciled after live-runtime and grace checks rather than failed immediately at startup. `codex_worker_inspect(view="integration_preview")` previews accepted-result application and `codex_worker_integrate` applies one accepted isolated worker result to the base checkout; `accepted_dirty_base` can allow known phase artifacts while still blocking unexpected dirty files. Codex auth/session startup is serialized per effective Codex home with a process-local gate plus host file lock, and spawned Codex CLI jobs receive that home as `CODEX_HOME`. One shared Server URL exposes shared local worker/job/artifact state to connected MCP sessions; tool mode is session-local, cross-owner worker/artifact mutation requires explicit `takeover: true`, and base-checkout mutation paths fail fast with `repo_busy`. Visible MCP `content` text for worker/status/report tools is compact while full data remains in `structuredContent`; optional Apps tool cards remain disabled by default. Automatic commits, merge queues, general message buses, queued worker-message delivery, active-turn steering, and app-server backend migration are not implemented yet. Direct MCP trial tooling is available through `scripts/real_mcp_worker_trial.py`, including the `--multi-client --tool-mode worker` scenario.

The existing workspace, low-level job, session, handoff, and power-tool surfaces remain available for compatibility and explicit control.

State-visibility details are recorded in
[2026-07-03_STATE_VISIBILITY_IMPLEMENTATION.md](2026-07-03_STATE_VISIBILITY_IMPLEMENTATION.md).

## ChatGPT Instruction Surface

The ChatGPT-facing prompt surface is the combination of MCP `initialize.instructions`, `tools/list` descriptors, annotations, output schemas, and the setup docs. The optional passive tool card exists for operator-enabled visual receipts, but it is off by default because repeated Apps cards made long ChatGPT sessions heavy on mobile and tablet browsers. Keep the default surface worker-first for first real ChatGPT validation:

- launch with `--tool-mode worker`;
- keep ChatGPT in the lead/manager/consultant role: use direct context tools for light orientation, worker briefing context, focused verification, and tiny exceptions, and delegate non-trivial repository work to workers instead of doing a manual line-by-line implementation loop;
- encourage worker teams for broad work. ChatGPT should use configured worker capacity rather than imposing an artificial one-or-two-worker limit, and should appoint parallel investigators, implementers, reviewers, verification workers, and synthesis workers when responsibilities are clear;
- use `codex_tool_mode_info` and `codex_tool_mode_switch` only for explicit, temporary broadening; ChatGPT may need connector refresh before newly exposed tools appear;
- start with `codex_self_test` and `codex_open_workspace`;
- treat one copied Server URL as one shared local state surface and use coordination-owner-relative ownership/takeover signals instead of assuming a private app instance; `active_mcp_sessions` is transport-session churn, not proof of worker ownership by itself;
- interpret `legacy_connection` as old unscoped durable metadata, not as proof that another ChatGPT owner controls the worker; after user confirmation, explicit takeover migrates it to the current scoped owner model;
- manage workers by human name instead of backend IDs;
- treat workers as continuing specialists and use `codex_worker_message` for follow-up before final synthesis when evidence is weak, contradictory, or decision-critical;
- ask for durable report files or changed-file evidence for consequential audits, implementation, review, or synthesis;
- inspect reports, changes, diffs, and `integration_preview` before integration;
- report `repo_busy` or path-guard setup failures directly instead of trying to bypass local controls;
- describe integration as explicit, no-commit, and preserving the worker worktree;
- report validation blockers instead of claiming unverified success.

## Product Principle

Natural language carries management. Codex performs local engineering work. PatchBay preserves continuity and performs exact mechanics.
