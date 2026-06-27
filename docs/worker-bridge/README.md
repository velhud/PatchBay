# Natural-Language Worker Bridge

Status: Phase 4 worker integration, artifact inbox transfer, and shared-server multi-client coordination implemented; Phase 4.5 direct MCP and tokenized public-tunnel artifact-flow validation evidence available; real ChatGPT Developer Mode UI validation remains blocked/pending; app-server backend phase pending.

This directory defines the worker layer for `patchbay`.

The current application exposes a local Streamable HTTP MCP bridge that lets ChatGPT inspect configured repositories, launch local Codex jobs, and manage named Codex workers. The worker layer keeps the existing foundation, but changes the normal product abstraction:

```text
Current: ChatGPT operates Codex jobs.
Phase 1: ChatGPT manages named read-only Codex workers.
Phase 2: ChatGPT manages named isolated writing workers.
Phase 3: ChatGPT coordinates several workers by passing bounded peer context.
Phase 4: ChatGPT explicitly previews and applies an accepted isolated worker result.
Post-Phase 4: ChatGPT imports generated files or zips into the worker artifact inbox.
Phase 4.5: Codex validates the complete worker bridge with broad deterministic, real-Codex, public-tunnel simulator, and ChatGPT-facing checks.
```

The worker layer lets ChatGPT brief local Codex workers in natural language, continue those workers by name, import generated files or zips as artifact context, inspect concise reports, and stop an active turn. Phase 2 adds worker-owned writing worktrees and worker-scoped change/diff inspection. Phase 3 adds bounded peer-worker context and a concise team report. Phase 4 adds explicit integration preview and accepted-result application through exact git mechanics.

## Read Order

1. [00 Overview](00_ARCHITECTURAL_OVERVIEW.md)
2. [01 Current State And Gaps](01_CURRENT_STATE_AND_GAPS.md)
3. [02 Target Architecture](02_TARGET_ARCHITECTURE.md)
4. [03 Public MCP Contract](03_PUBLIC_MCP_CONTRACT.md)
5. [04 Runtime State Schema](04_RUNTIME_STATE_SCHEMA.md)
6. [05 End-To-End Algorithms](05_END_TO_END_ALGORITHMS.md)
7. [06 Implementation Phases](06_IMPLEMENTATION_PHASES.md)
8. [07 Repository Change Map](07_REPOSITORY_CHANGE_MAP.md)
9. [08 Testing And Release](08_TESTING_AND_RELEASE.md)
10. [09 Phase Package Protocol](09_PHASE_PACKAGE_PROTOCOL.md)
11. [10 Decisions Risks And Deferred Work](10_DECISIONS_RISKS_AND_DEFERRED.md)
12. [Phase 1 Durable Workers](PHASE1_DURABLE_WORKERS.md)
13. [Phase 2 Writing Workers](PHASE2_WRITING_WORKERS.md)
14. [Phase 3 Multi-Worker Coordination](PHASE3_MULTI_WORKER_COORDINATION.md)
15. [Phase 4 Integration](PHASE4_INTEGRATION.md)
16. [Multi-Chat Concurrency Plan](MULTI_CHAT_CONCURRENCY_PLAN.md)

## Implementation Status

The current worker surface stays small while covering setup options, artifact transfer, lifecycle, inspection, and explicit integration:

- `codex_worker_options`;
- `codex_worker_inbox`;
- `codex_worker_start`;
- `codex_worker_message`;
- `codex_worker_list`;
- `codex_worker_inspect`;
- `codex_worker_integrate`;
- `codex_worker_stop`.

The implementation derives worker identity and reports from existing durable job records and Codex session references. Worker display names are scoped to the base workspace, so the same human name can exist in separate repos. Default workers use one external isolated worktree across turns and PatchBay restarts. Worker change, file, and diff inspection is available on demand; before integration, use `codex_worker_inspect(view="file", file_path="...")` for worker-created file content because `codex_read_file` reads the base checkout. `codex_worker_options` provides a bounded model/reasoning menu from the installed Codex runtime/catalog; `codex_worker_inbox` imports ChatGPT-generated files/zips into local runtime storage and returns artifact ids; `codex_worker_start` and `codex_worker_message` can then set or inherit `model` and `reasoning_effort` while also including bounded report/change/diff context from other workers and selected artifacts. Imported artifacts are copied into `.ai-bridge/imported-artifacts/` inside isolated worker worktrees and excluded from integration. `codex_worker_list` returns a compact `team_report`. `codex_worker_inspect(view="integration_preview")` previews accepted-result application and `codex_worker_integrate` applies one accepted isolated worker result to the base checkout. One shared Server URL exposes shared local worker/job/artifact state to connected MCP sessions; tool mode is session-local, cross-owner worker/artifact mutation requires explicit `takeover: true`, and base-checkout mutation paths fail fast with `repo_busy` when another write is active. Automatic commits, merge queues, general message buses, and app-server backend migration are not implemented yet. Direct MCP trial tooling is available through `scripts/real_mcp_worker_trial.py`, including the `--multi-client --tool-mode worker` scenario.

The existing workspace, low-level job, session, handoff, and power-tool surfaces remain available for compatibility and explicit control.

## ChatGPT Instruction Surface

The ChatGPT-facing prompt surface is the combination of MCP `initialize.instructions`, `tools/list` descriptors, annotations, output schemas, the passive tool card, and the setup docs. Keep it worker-first for first real ChatGPT validation:

- launch with `--tool-mode worker`;
- use `codex_tool_mode_info` and `codex_tool_mode_switch` only for explicit, temporary broadening; ChatGPT may need connector refresh before newly exposed tools appear;
- start with `codex_self_test` and `codex_open_workspace`;
- treat one copied Server URL as one shared local state surface and use session-relative ownership/takeover signals instead of assuming a private app instance;
- manage workers by human name instead of backend IDs;
- inspect reports, changes, diffs, and `integration_preview` before integration;
- report `repo_busy` or path-guard setup failures directly instead of trying to bypass local controls;
- describe integration as explicit, no-commit, and preserving the worker worktree;
- report validation blockers instead of claiming unverified success.

## Product Principle

Natural language carries management. Codex performs local engineering work. PatchBay preserves continuity and performs exact mechanics.
