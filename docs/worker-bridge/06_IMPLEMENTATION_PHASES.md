# Implementation Phases

Status: Phase 4 integration preview and accepted-result application implemented; app-server backend phase not implemented yet.

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
-> restart wrapper
-> message worker by name
-> continue same Codex conversation
```

Default: read-only. This proves identity and conversation before worktree ownership.

Public capabilities:

- `codex_worker_start`;
- `codex_worker_message`;
- `codex_worker_list`;
- `codex_worker_inspect` for `status` and `report`;
- `codex_worker_stop`.

No integration tool yet.

Added:

- `worker_runtime.py`;
- `tests/test_worker_runtime.py`;
- `tests/test_worker_tools.py`.
- `tests/test_worker_tool_surface.py`;
- `worker_tool_surface.py`;
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
- `codex_worker_list` returns `team_report`.

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

## Phase 5: App-Server Backend And Real ChatGPT Release Validation

Goal:

- evaluate official Codex app-server behind the same public worker contract;
- complete real ChatGPT Developer Mode worker flows;
- complete token-gated tunnel eval if needed;
- update readiness statements based on evidence.

Do not change the public worker contract for this backend migration.

## Dependency Graph

```text
Phase 0 docs
  -> Phase 1 worker identity/conversation
    -> Phase 2 persistent worker worktrees
      -> Phase 3 multi-worker UX
        -> Phase 4 integration
          -> Phase 5 app-server optimization + release evidence
```

The Phase 5 app-server spike may start after Phase 1, but it must not destabilize the worker contract or block Phases 2-4.
