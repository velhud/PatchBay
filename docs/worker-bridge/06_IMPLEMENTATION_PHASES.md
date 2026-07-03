# Implementation Phases

Status: historical implementation sequence. The current runtime includes integration preview, accepted-result application, artifact inbox transfer, and shared-server multi-client coordination; app-server backend work is not implemented.

## Delivery Strategy

The worker bridge should be delivered in independently reviewable phases. Later phases may refactor internals, but public behavior introduced by an accepted phase should remain stable unless explicitly migrated.

## Phase 0: Architecture Documentation

Status: complete.

Goal:

- freeze the product model;
- document the planned public worker contract;
- document state ownership;
- document algorithms;
- document testing and release gates;
- avoid code behavior changes.

Exit criteria:

- tracked public docs exist;
- docs are sanitized and do not include private planning material;
- Phase 1 implementer can start without redefining architecture.

## Phase 1: Durable Single-Worker Conversation

Status: implemented.

Goal:

```text
start worker
-> complete first Codex turn
-> persist identity/session/report
-> restart PatchBay
-> message worker by name
-> continue same Codex conversation
```

Default: read-only. This proves identity and conversation before worktree ownership.

Public capabilities:

- `codex_worker_start`;
- `codex_worker_message`;
- `codex_worker_list`;
- `codex_worker_status`;
- `codex_worker_inspect` for `status` and `report`;
- `codex_worker_stop`.

No integration tool yet.

Added:

- `src/patchbay/workers/runtime.py`;
- `tests/test_worker_runtime.py`;
- `tests/test_worker_tools.py`.
- `tests/test_worker_tool_surface.py`;
- `src/patchbay/workers/tool_surface.py`;
- `scripts/worker_phase1_eval.py`;
- `docs/worker-bridge/PHASE1_DURABLE_WORKERS.md`.

Modified:

- job manager cleanup retention;
- tools and MCP protocol descriptors;
- tool card resources;
- public docs and eval docs.

Architecture correction:

- worker identity is private metadata on durable job records;
- Codex sessions remain the conversation source;
- no separate worker database or busy-worker queue is implemented.

Exit criteria:

- one named worker survives restart and can be continued naturally;
- ChatGPT does not need job or session IDs;
- existing baseline checks and live MCP eval pass;
- no worker worktree behavior changes yet.

## Phase 2: Worker-Owned Worktrees And Writing Continuity

Status: implemented.

Goal:

```text
one writing worker
-> one stable isolated worktree
-> multiple natural-language turns
-> isolated and inspectable changes
```

Public changes:

- `workspace_mode` supports `isolated_write`, `read_only`, and `shared_write`;
- `codex_worker_inspect(view="changes")` is available;
- `codex_worker_inspect(view="diff", file_path="...")` is available;
- `codex_worker_stop(cleanup_workspace=true)` discards an isolated worker worktree.

Required behavior:

- external worker worktrees under private runtime state;
- predictable worker branch naming;
- base revision persistence;
- worker-owned worktrees excluded from job cleanup;
- explicit cleanup path;
- change inventory from git, not only model self-report.

Added:

- `scripts/worker_phase2_eval.py`;
- `tests/test_worker_resume_command.py`;
- `docs/worker-bridge/PHASE2_WRITING_WORKERS.md`.

Modified:

- worker runtime workspace-mode handling;
- job manager external worker worktree creation/cleanup;
- job executor resume command ordering;
- worker tool schemas, annotations, and output fields;
- public docs and testing docs.

Exit criteria:

- a writing worker can work and revise without creating a new worktree per message;
- main repository remains unchanged until explicit integration;
- existing apply-job behavior remains compatible.

## Phase 3: Multi-Worker Coordination And Worker-First UX

Status: implemented.

Goal:

- coordinate several active workers naturally;
- route bounded report/change/diff context from one worker into another worker turn;
- expose a concise team view;
- keep worker-first UX as the recommended normal path;
- avoid creating a worker ERP, message bus, role engine, or generic workflow graph.

Public changes:

- `codex_worker_start(context_from_workers=[...], context_detail="report|changes|diff")`;
- `codex_worker_message(context_from_workers=[...], context_detail="report|changes|diff")`;
- `codex_worker_status` returns compact team status;
- `codex_worker_list` returns `team_status` / `team_report`.

Added:

- `scripts/worker_phase3_eval.py`;
- `tests/test_worker_coordination.py`;
- `docs/worker-bridge/PHASE3_MULTI_WORKER_COORDINATION.md`.

Modified:

- worker runtime peer-context construction;
- worker tool schemas and output fields;
- tool-card labels;
- MCP initialize instructions;
- public docs and testing docs.

Exit criteria:

- ChatGPT can coordinate at least two workers naturally;
- a reviewer can receive another worker's report or bounded diff;
- a worker can receive another worker's report as natural-language feedback;
- team status is readable without job/session ids;
- no generic event bus, mailbox, role engine, or automatic merge has been introduced.

## Phase 4: Integration Preview And Accepted-Result Application

Status: implemented.

Goal:

- inspect and compare worker results;
- preview worker patches safely;
- apply an explicitly accepted worker result;
- preserve rejected work until cleanup.

Public changes:

- `codex_worker_inspect(view="integration_preview")`;
- `codex_worker_integrate`;
- full cleanup support through `codex_worker_stop(cleanup_workspace=true)`.

Exit criteria:

- accepted worker output can be applied without manual patch copying;
- conflicts are reported without target mutation;
- rejected work remains available.

## Post-Phase 4: Artifact Inbox Transfer

Status: implemented.

Goal:

- let ChatGPT transfer generated files or zip packages to local PatchBay runtime storage;
- attach selected artifact ids to isolated workers as source material;
- support repeated imports in one conversation;
- keep imported artifacts out of base-checkout changes, worker diffs, integration previews, and applies.

Public changes:

- `codex_worker_inbox`;
- `context_from_artifacts` on `codex_worker_start`;
- `context_from_artifacts` on `codex_worker_message`.

Exit criteria:

- imports do not edit the repo;
- direct `file://` imports are rejected by default;
- archive traversal and link/device entries are rejected;
- attached artifacts are materialized only inside `.ai-bridge/imported-artifacts/` in isolated worker worktrees;
- direct tokenized public-tunnel MCP simulation proves import, worker read, exclusion, and cleanup.

## Post-Phase 4: Shared-Server Multi-Client Coordination

Status: implemented.

Goal:

- let multiple ChatGPT conversations or MCP clients connect to one local Server URL without accidentally broadening each other's tool surface or mutating each other's workers/artifacts;
- keep read/list/inspect shared, because this is a local operator tool rather than a multi-tenant SaaS boundary;
- require explicit takeover for cross-owner worker/artifact mutation;
- serialize base-checkout mutation per repository without adding a hidden queue.

Public changes:

- `codex_self_test` returns safe shared-server coordination metadata such as `client_ref` and active session count;
- `codex_tool_mode_switch` applies to the current MCP session rather than every connected session;
- worker and artifact views expose safe session-relative ownership fields without raw MCP session ids;
- mutating worker/artifact calls can return `takeover_required` and accept explicit `takeover: true`;
- base-checkout mutation paths can return `repo_busy: true` when another write is active.

Exit criteria:

- direct multi-client MCP trial passes with two logical sessions;
- session A switching tool mode does not change session B's catalog;
- cross-owner mutation refuses until explicit takeover;
- same-repo base mutation conflicts fail fast, while unrelated repo work can continue;
- public outputs do not include raw MCP session ids, private paths, or backend worker/job identifiers.

## Phase 5: App-Server Backend And Real ChatGPT Release Validation

Goal:

- evaluate official Codex app-server behind the same public worker contract;
- complete real ChatGPT Developer Mode worker and artifact flows from the actual UI;
- complete ChatGPT-originated token-gated tunnel eval if needed;
- keep the direct tokenized public-tunnel MCP simulator as a regression check;
- update readiness statements based on evidence.

Do not change the public worker contract for this backend migration.

## Dependency Graph

```text
Phase 0 docs
  -> Phase 1 worker identity/conversation
    -> Phase 2 persistent worker worktrees
      -> Phase 3 multi-worker UX
        -> Phase 4 integration
          -> artifact inbox transfer
          -> Phase 5 app-server optimization + release evidence
```

The Phase 5 app-server spike may start after Phase 1, but it must not destabilize the worker contract or block Phases 2-4.
