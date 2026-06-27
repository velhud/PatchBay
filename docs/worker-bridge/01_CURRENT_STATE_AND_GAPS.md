# Current State And Gaps

Status: Phase 2 isolated writing workers implemented; integration gaps remain.

## Current Component Model

The current application already has this shape:

```text
ChatGPT or MCP client
  -> FastAPI Streamable HTTP connector
  -> public tool registry and schema policy
  -> workspace context tools
  -> Codex job/session orchestration
  -> Codex CLI subprocesses
  -> durable job records, logs, worktrees, and diffs
```

## Existing Strengths To Preserve

Connector and MCP:

- `/mcp` Streamable HTTP transport;
- MCP session handling;
- request-size limits;
- ChatGPT/App descriptors and annotations;
- passive result-card resource;
- token-gated tunnel behavior;
- local doctor/start/profile tooling.

Workspace and context:

- allowed repository roots;
- path validation and symlink escape rejection;
- blocked secret/cache/build globs;
- bounded tree, read, and search;
- git status and diff;
- AGENTS and Skill discovery/loading;
- `.ai-bridge` context and handoff artifacts.

Codex execution:

- async jobs;
- plan, apply, review, interactive, resume, and cancellation paths;
- restricted subprocess environment;
- prompt transport over stdin;
- Codex JSONL result parsing;
- session ID extraction;
- durable redacted job state.

Isolation and evidence:

- isolated apply-job worktrees;
- diff retrieval restricted to completed apply jobs and proven changed files;
- bounded and redacted stdout/stderr artifacts;
- default-denied direct write, bash, and transcript-read power modes unless explicitly configured.

## Gap Between Current And Full Target Product

### Job-Centric Public Model

Current flow:

```text
start job -> poll status -> get result -> get diff -> remember session ID -> resume
```

Phase 1 flow:

```text
start worker -> message worker -> read report -> stop active turn when needed
```

Phase 2 flow:

```text
start isolated worker -> write in worker worktree -> message worker -> inspect changes/diff
```

### Durable Human Worker Identity

Phase 1 tags durable job records with private worker id/name fields so several jobs can belong to a colleague named `Connector Investigator`.

Session listing remains useful metadata, but the worker facade now provides identity, assignment continuity, natural-language addressing, and isolated worker worktree ownership.

### Minimal Wrapper-Level Conversation State

Durable continuation still depends on job records and Codex session IDs, but the public surface can now address a worker by name and hide those backend references.

### Job-Scoped Worktrees

The existing apply-job worktree is owned by one apply job. A writing worker now owns one stable external worktree across multiple turns.

### No Worker-Level Integration Path

The wrapper can expose a job diff. It cannot yet compare a worker worktree against the target workspace, check whether a patch applies cleanly, explain overlapping changes, or apply accepted work.

### Broad Default Tool Catalog

The current full tool mode exposes canonical tools and compatibility aliases. This is useful for debugging and power use. The worker-first mode advertises a smaller natural delegation surface.

## Architectural Conclusion

The correct move is extension, not rewrite:

```text
Keep:
connector + tools + jobs + executor + workspace context + security + artifacts

Added through Phase 2:
worker facade + worker mapping via durable jobs + durable isolated writing worktrees

Still later:
integration
```
