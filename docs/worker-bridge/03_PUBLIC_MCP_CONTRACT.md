# Public MCP Contract

Status: current worker contract implemented; app-server backend pending.

## Design Rule

Public worker tools should represent distinct managerial acts, not internal implementation nouns.

The implemented worker surface is:

```text
codex_worker_options
codex_worker_inbox
codex_worker_start
codex_worker_message
codex_worker_list
codex_worker_inspect
codex_worker_integrate
codex_worker_stop
```

Existing low-level `codex_*` tools remain stable and available for current use, debugging, compatibility, and power-user workflows.

## Implemented Worker Tools

### `codex_worker_options`

Purpose: return a bounded setup menu for selecting a Codex model and reasoning effort before starting or intentionally retuning a worker.

Behavior loads model metadata from the installed Codex runtime/catalog when available and returns only safe public fields. It does not expose raw Codex config paths, provider/auth settings, prompts, base instructions, or full catalog blobs.

### `codex_worker_inbox`

Purpose: import ChatGPT-generated files or zip packages into local PatchBay runtime storage, then return artifact ids that can be attached to isolated workers.

Input for import:

```json
{
  "action": "import_file",
  "artifact_file": "{ChatGPT Apps file parameter}",
  "label": "optional short label"
}
```

Other actions: `list`, `inspect`, and `cleanup`.

Importing an artifact does not edit the repository. `codex_worker_start` and `codex_worker_message` accept `context_from_artifacts` to copy selected artifacts into `.ai-bridge/imported-artifacts/` inside an isolated worker worktree. That reserved directory is excluded from worker changes, diffs, integration preview, and apply.

### `codex_worker_start`

Purpose: create a named worker and send its first natural-language assignment.

Input:

```json
{
  "name": "Connector Investigator",
  "brief": "Inspect continuation behavior and report the smallest clean fix.",
  "repo_path": "/optional/allowed/repo",
  "workspace_mode": "isolated_write",
  "context_from_artifacts": ["art_example123"]
}
```

Required fields: `name`, `brief`.

`workspace_mode` is optional. The default is `isolated_write`, which creates a private external git worktree and runs Codex with workspace-write authority inside it. `read_only` is available for advisory workers. `shared_write` is explicit direct-workspace mode.

Normal output must not expose session IDs, job IDs, process IDs, branch names, or private repository/worktree paths.

### `codex_worker_message`

Purpose: send a natural-language continuation, correction, question, or attributed peer message to a worker.

Input:

```json
{
  "worker": "Connector Investigator",
  "message": "Keep the current architecture. Do not create a new persistence subsystem."
}
```

Behavior:

- if idle, start a continuation immediately;
- if running, return `accepted: false` with a clear explanation;
- never require the caller to supply a session ID.

### `codex_worker_list`

Purpose: return the current local worker set in a compact, human-usable form.

Output fields:

- worker ID;
- name;
- state;
- workspace ID and display name;
- latest report;
- session availability;
- whether the worker can receive a follow-up;
- updated timestamp.

### `codex_worker_inspect`

Purpose: read a worker's current state, latest report, changed-file inventory, worker-side file content, one-file diff, or integration preview.

Input:

```json
{
  "worker": "Connector Investigator",
  "wait_seconds": 0,
  "view": "report"
}
```

Implemented views:

- `report` / `status`: current public worker view and latest report.
- `changes`: changed-file inventory from the worker workspace.
- `file`: bounded text content for a workspace-relative `file_path` inside the worker workspace. Use this before integration for files created by the worker.
- `diff`: bounded unified diff for a workspace-relative `file_path`.
- `integration_preview`: read-only preview of whether an isolated writing worker's result can apply to the base checkout.

`codex_read_file` reads the base checkout, not the isolated worker worktree. Before integration, use `view=file` or `view=diff` on `codex_worker_inspect` for worker-created files.

### `codex_worker_integrate`

Purpose: apply an explicitly accepted worker result to its base workspace after a clean preview.

This applies the worker patch to the base checkout only after preview succeeds. It does not commit, delete the worker worktree, auto-resolve conflicts, or create a PR.

### `codex_worker_stop`

Purpose: interrupt active work.

Behavior preserves the worker identity and prior conversation reference. `cleanup_workspace=true` explicitly discards an isolated worker worktree and private branch. After cleanup, the worker history remains inspectable but PatchBay refuses to continue that worker instead of falling back to the base checkout.

## Worker Tool Mode

The `worker` tool mode advertises:

- `codex_self_test`;
- workspace/context essentials;
- the worker tools above.

It should exclude:

- low-level job lifecycle tools;
- compatibility aliases;
- direct write/bash/session transcript power tools.

The default ChatGPT-facing mode is `worker`. Full mode remains available for
deliberate power-user and compatibility runs, but a real ChatGPT run that falls
back into broad manual reading is evidence against advertising full mode by
default, not evidence against worker mode.

## Why Six Tools

One universal action tool would mix read-only and mutating operations under one descriptor.

A large tool list would expose internal nouns and add tool-selection noise.

Six implemented tools preserve meaningful boundaries while keeping the surface managerial. Integration is explicit because accepting a worker result is a distinct mutating management act.
