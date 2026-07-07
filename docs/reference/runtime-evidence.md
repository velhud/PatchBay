# Runtime Evidence And Logging

PatchBay has two different logging jobs:

1. Keep the public/operator surfaces compact, stable, and safe to inspect.
2. Preserve enough private runtime evidence to reconstruct what ChatGPT asked,
   what PatchBay launched, what Codex received, what workers answered, and what
   state transitions happened.

These must not be collapsed into one giant dump. They are separate artifacts
under `PATCHBAY_HOME/runtime` by default.

## Artifact Layers

### Compact audit log

Path: `runtime/logs/audit.log`

Purpose:

- one line per MCP request;
- timestamp, client ref, JSON-RPC method, request id, and tool name;
- fast timeline reconstruction;
- no prompt bodies by default.

This is the first file to read when asking "what happened when?"

### Private MCP transcripts

Path: `runtime/logs/private-evidence/mcp/<YYYY-MM-DD>/<client-ref>.jsonl`

Purpose:

- full MCP request bodies from ChatGPT or another MCP client;
- full MCP responses from PatchBay;
- exact worker-start briefs, worker-message texts, stop reasons, integration
  requests, status calls, tool options, and errors;
- grouped by day and client ref so evidence is organized without becoming
  hundreds of unrelated files.

This is disabled unless `logging.private_evidence_log`,
`logging.store_mcp_transcripts`, `logging.log_prompt_bodies`, or
`logging.log_response_bodies` is enabled.

### Job state records

Path: `runtime/logs/jobs/state/<job-id>.json`

Purpose:

- durable job lifecycle state;
- worker name/id, workspace mode, model, reasoning, repo/worktree references,
  liveness fields, checkpoint summaries, result summary, and terminal status;
- prompt hash/byte count/private artifact pointer when private job prompt
  evidence is enabled.

The job state record intentionally does not contain the prompt body. It points to
private evidence when that evidence exists.

### Job brief evidence

Path: `runtime/logs/private-evidence/jobs/<job-id>/brief.json`

Purpose:

- full prompt/brief passed into the Codex job;
- job mode, repo path, worktree path, branch name, options, model/reasoning,
  worker metadata, prompt hash, and prompt byte count;
- the exact managerial contract for the worker.

This is the source to inspect when asking why a worker was started, why a model
was selected, whether ChatGPT gave a sequential or parallelizable task, or
whether a worker received enough context.

This is disabled unless `logging.private_evidence_log` or
`logging.store_job_prompts` is enabled.

### Codex stdout/stderr event artifacts

Path:

- `runtime/logs/jobs/<job-id>_stdout.log`
- `runtime/logs/jobs/<job-id>_stderr.log`

Purpose:

- Codex JSON event stream;
- command start/completion events;
- agent messages/checkpoints when Codex emits them;
- command output previews;
- startup/session diagnostics and stderr.

By default these artifacts are redacted and capped by
`logging.job_log_max_bytes`. Set `logging.write_raw_job_logs: true` only for a
trusted private runtime where full raw streams are required.

### Structured result artifacts

Path: `runtime/logs/jobs/<job-id>_result.json`

Purpose:

- parsed final structured worker result when available;
- fallback result if Codex completed without a structured final report;
- partial/cancellation result when a worker is stopped;
- files changed, tests run, evidence, risks, and notes when the worker emitted
  them.

### Worker worktrees

Path: `PATCHBAY_HOME/worktrees/worker-<worker-id>/...`

Purpose:

- durable isolated worker checkout;
- unintegrated file changes;
- report files written by the worker;
- review/integration evidence.

This is not a log file, but it is part of the evidence model. Do not delete it
when investigating a worker result unless cleanup is the explicit task.

### Hub/edge state

Path: `runtime/hub/hub-state.json`

Purpose:

- enrolled machines;
- queued/running/completed hub commands;
- compact hub events.

Hub state is a coordination projection. Edge machines remain the source of truth
for local Codex auth, repositories, worker state, worktrees, and private logs.

## Configuration

```yaml
logging:
  audit_file:
  job_logs_dir:
  job_state_dir:
  private_evidence_dir:
  job_log_max_bytes: 200000
  write_raw_job_logs: false
  access_log: false
  private_evidence_log: false
  store_job_prompts: false
  store_mcp_transcripts: false
  log_prompt_bodies: false
  log_response_bodies: false
```

Recommended modes:

- Public/default: leave private evidence disabled.
- Private personal VM/workbench: set `private_evidence_log: true` so full worker
  briefs and MCP request/response bodies are preserved.
- Debug only job prompts: set `store_job_prompts: true`.
- Debug only ChatGPT/PatchBay tool exchanges: set `store_mcp_transcripts: true`.

## Debugging Questions This Enables

- Did ChatGPT act as manager or manual executor?
- Did ChatGPT start workers in parallel or sequentially?
- What exact brief did a worker receive?
- Was a task actually dependent on a prior integration, or could it have been
  parallelized?
- Which model/reasoning/workspace mode was selected and why?
- Did PatchBay pass the prompt to Codex exactly as intended?
- Did Codex produce a final report, only raw output, or partial checkpoints?
- Was a stop/cancel justified by worker state, or did ChatGPT misunderstand the
  liveness surface?
- Did an OpenAI/platform safety block happen before PatchBay received the call,
  inside a PatchBay tool response, or while inspecting large output?

## Boundary

Private evidence can contain prompts, private paths, repo names, worker briefs,
and user-provided material. It must stay in the private runtime area and should
not be committed, mirrored into public docs, or surfaced through ordinary
ChatGPT tools. ChatGPT-facing status should remain compact and managerial; deep
evidence inspection is a maintainer/debugging action.
