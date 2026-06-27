# Repository Change Map

Status: Phase 3 multi-worker coordination implemented; integration files pending.

## Architectural Rule

Extend current modules around their existing responsibilities. Do not replace the working connector, workspace context layer, or job executor.

## Phase 1 New Files

```text
src/patchbay/workers/runtime.py
src/patchbay/workers/tool_surface.py
scripts/worker_phase1_eval.py
tests/test_worker_runtime.py
tests/test_worker_tools.py
tests/test_worker_tool_surface.py
docs/worker-bridge/PHASE1_DURABLE_WORKERS.md
```

## Phase 2 New Files

```text
scripts/worker_phase2_eval.py
tests/test_worker_resume_command.py
docs/worker-bridge/PHASE2_WRITING_WORKERS.md
```

## Phase 3 New Files

```text
scripts/worker_phase3_eval.py
tests/test_worker_coordination.py
docs/worker-bridge/PHASE3_MULTI_WORKER_COORDINATION.md
```

## Expected Later Files

Later implementation phases may add:

```text
worker_integration.py
codex_backends.py       # only if app-server is adopted
codex_app_server.py     # only if app-server is adopted
```

Likely tests:

```text
tests/test_worker_integration.py
tests/test_codex_app_server.py  # conditional
```

`src/patchbay/workers/runtime.py` currently contains worker resolution, workspace-mode handling, dispatch, report projection, change/diff views, peer-context construction, team-report projection, and public view logic without a separate worker store/mailbox/status/artifact module.

## Existing Files Expected To Change Later

### MCP Protocol

Phase 2 keeps the five worker tools, adds workspace-mode and change/diff fields, updates mutability annotations for default writing workers, and preserves existing low-level tools.

Do not mix read-only and mutating worker actions under one descriptor.

### Tool Handler

Worker calls route into `WorkerRuntime`, return public worker views instead of private records, and keep existing job/context handlers stable.

### Job Manager

Private worker association and workspace metadata stay in durable job options. Phase 2 distinguishes one-shot apply worktrees from durable worker worktrees while keeping loading backward compatible.

Do not add a separate worker store until the current durable-job-derived model proves insufficient.

### Job Executor

Phase 2 reuses existing interactive/resume execution and reasserts `--sandbox` and `--cd` before the `resume` subcommand. Later phases must keep current command building, environment restriction, timeout, cancellation, redaction, and JSONL parsing.

### Server

Phase 1 derives worker state lazily from durable jobs and does not require server startup reconciliation or a second HTTP API.

### Config

Phase 2 adds a small `workers` section:

```yaml
workers:
  worktree_root: ""
```

Empty paths should resolve under private runtime state.

Do not add a large worker policy matrix.

### Workspace Context

Expected changes are small or none. Do not weaken allowed-root or blocked-glob behavior to expose worker worktrees generically.

### Tool Resources

Phase 2 renders worker status/report/change previews without raw private paths or backend IDs.

### Live MCP Eval

Phase 2 adds targeted worker tests and a real-Codex isolated writing eval script. Later phases should extend real ChatGPT scenarios and integration checks. Real Codex and real ChatGPT scenarios must remain clearly labeled as separate from local smoke probes.

## Files Not To Remove

Do not delete or replace:

- current low-level Codex job tools;
- `.ai-bridge` handoff system;
- workspace context tools;
- power tools;
- profile/tunnel/connector system;
- current security documentation and tests;
- CodexPro attribution.

These are complementary surfaces.

## Suggested Dependency Shape

```text
src/patchbay/server.py
  -> JobManager
  -> JobExecutor
  -> WorkerRuntime
       -> worker persistence
       -> worker worktrees
       -> worker integration
       -> Codex backend
  -> ToolHandler
  -> MCPProtocol
```

Avoid circular ownership:

- `WorkerRuntime` may call job/executor services;
- `JobManager` must not import `WorkerRuntime`;
- public protocol code must not perform git or persistence operations directly.
