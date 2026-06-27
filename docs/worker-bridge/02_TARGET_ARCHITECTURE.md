# Target Architecture

Status: Phase 2 isolated writing worker architecture implemented; integration architecture pending.

## Top-Level Shape

```text
User
  -> ChatGPT as lead
  -> natural-language worker MCP tools
  -> worker facade
  -> worker runtime
  -> existing Codex job/session execution
  -> local Codex + repository + worktrees
```

## Architectural Layers

### Existing Connector Layer

Unchanged responsibility:

- FastAPI server;
- MCP protocol;
- auth;
- tool descriptors;
- resources;
- session headers;
- request limits.

### Existing Workspace Context Layer

Unchanged responsibility:

- allowed roots;
- safe orientation;
- read/search/git/AGENTS/Skills;
- bounded context and handoff artifacts.

### Worker Facade

Current responsibility:

- expose a small ChatGPT-facing vocabulary;
- accept natural-language briefs and feedback;
- resolve workers by human name or ID;
- hide job IDs, session IDs, worktree paths, and backend details by default;
- return concise worker state and reports.

The facade does not plan the work. ChatGPT and Codex do that.

### Worker Runtime

Current responsibility:

- derive worker groups from private durable job metadata;
- map worker identity to session, job, and repository;
- dispatch new turns and continuations;
- reject follow-ups while a worker is busy without creating a queue;
- project completed jobs into the worker's latest report;
- stop work;
- expose state/report views.

The implementation begins as one cohesive module, not multiple speculative services.

### Codex Conversation Backend

The worker runtime should use a small internal backend contract:

```text
start(worker, message) -> execution reference
continue(worker, message) -> execution reference
stop(worker) -> result
reconcile(worker) -> backend state/result
```

Backend V1 uses the existing `codex exec` and resume machinery because it is already implemented and tested.

Backend V2 may use the official Codex app-server after a local spike. The public worker interface must remain backend-neutral.

### Worker Workspace Manager

Current responsibility:

- create one stable isolated worktree for a writing worker;
- reuse it across worker turns;
- record base revision and branch;
- keep it separate from job cleanup;
- inspect changes;
- remove it only through explicit worker cleanup;
- leave exact integration preview and apply to a later phase.

Recommended private layout:

```text
$CODEX_MCP_HOME/
  worktrees/
    worker-<worker-id>/
```

## Worker Workspace Modes

| Mode | Meaning | Default use |
| --- | --- | --- |
| `isolated_write` | Stable per-worker worktree; worker may write. | Default implementation worker mode. |
| `read_only` | Main repository with read-only Codex sandbox. | Investigation, review, advisory work. |
| `shared_write` | Main repository with workspace-write access. | Explicit exception only. |

No deterministic role classifier is required. ChatGPT or the caller can choose the workspace mode as part of management.

## Context Flow

ChatGPT sends a task-specific natural-language brief, not its entire conversation.

The first worker turn receives short stable framing plus the brief:

```text
You are the local Codex worker "<name>" reporting to ChatGPT as engineering lead.
Work autonomously inside the assigned repository or worktree. Follow repository
instructions and use your tools normally. At the end, return a concise
engineering report: outcome, meaningful verification, unresolved uncertainty,
and recommended next step. Do not dump full logs or diffs unless they are the
actual result.

Assignment:
<ChatGPT brief>
```

Follow-up turns normally contain only the new natural-language instruction. The Codex session carries prior context.

## Canonical Truth Boundaries

- Worker-tagged durable job metadata is identity and routing truth.
- Job records are execution-state truth.
- Codex session/thread is conversation-history truth.
- Git worktree is code-state truth.
- Job logs and diffs are evidence truth.
- ChatGPT is management and final synthesis.
