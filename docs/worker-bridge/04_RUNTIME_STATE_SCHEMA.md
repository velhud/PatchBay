# Runtime State Schema

Status: Phase 2 uses durable job metadata, including worker workspace metadata; separate worker records are deferred.

## State Philosophy

Do not build a second platform that duplicates Codex, git, and the existing job system.

The worker layer needs only enough durable state to answer:

```text
Who is this worker?
Where does it work?
Which Codex conversation belongs to it?
What is it doing now?
What did it last report?
Where are its current code changes?
```

## Canonical Sources Of Truth

| Concern | Canonical source |
| --- | --- |
| Human-facing worker identity | Private worker metadata on durable job records |
| Current execution state | Existing job record or backend turn state |
| Conversation history | Codex thread/session |
| Code changes | Worker-owned Git worktree for `isolated_write`, base checkout for `shared_write` |
| Logs and structured result | Existing job artifacts/result |
| User decision and management context | ChatGPT conversation |

The current implementation does not create a separate worker record. It stores private worker identity and workspace metadata inside durable job options, then derives the public worker view from those jobs. Future worker records, if added, should store references and a concise latest report without copying full transcripts or full artifacts.

## Private Runtime Layout

```text
$PATCHBAY_HOME/
  worktrees/
    worker-<worker-id>/
```

Worker files should be written atomically and privately where the platform permits.

## Durable Job Metadata

Worker-tagged jobs store private options:

```json
{
  "_worker_id": "wrk_...",
  "_worker_name": "Connector Investigator",
  "_worker_workspace_mode": "isolated_write",
  "_worker_base_repo_path": "<private absolute authorized repo path>",
  "_worker_worktree_path": "<private absolute external worktree path>",
  "_worker_branch_name": "codex/worker-...",
  "_worker_base_revision": "<git sha>",
  "sandbox": "workspace-write",
  "structured_output": true,
  "json_events": true,
  "resume_session_id": "only on continuation jobs"
}
```

Rules:

- keep `_worker_id`, `_worker_name`, job IDs, session IDs, repo paths, and worktree paths out of normal public worker output;
- retain worker-tagged jobs during ordinary age cleanup because they are the minimal durable identity/session index;
- never copy raw Codex transcripts into worker state.

## Future Worker Record V1

Potential later fields:

```json
{
  "version": 1,
  "worker_id": "wrk_...",
  "name": "Connector Investigator",
  "workspace_id": "ws_...",
  "repo_path": "<private absolute authorized repo path>",
  "workspace_mode": "read_only",
  "worktree_path": null,
  "branch_name": null,
  "base_revision": null,
  "session_id": "optional Codex session id",
  "active_job_id": null,
  "last_job_id": null,
  "state": "idle",
  "latest_report": "Bounded natural-language worker report.",
  "latest_result": {
    "files_changed": [],
    "commands_run": [],
    "tests_run": [],
    "notes": "",
    "next_steps": []
  },
  "parent_worker_id": null,
  "created_at": "timestamp",
  "updated_at": "timestamp",
  "stopped_at": null
}
```

## Field Rules

- `worker_id` is opaque and stable.
- `name` is human-readable and not a hardcoded role.
- `repo_path`, `worktree_path`, job IDs, and session IDs are private implementation fields and should be omitted from ordinary public MCP output.
- `workspace_mode` is one of `isolated_write`, `read_only`, or `shared_write`.
- `state` should stay small: `starting`, `working`, `idle`, `failed`, `stopped`.
- Semantic judgments such as blocked, needs review, or good implementation belong in the worker report, not mandatory state transitions.
- `latest_report` is bounded and redacted so it remains useful after old job artifacts are cleaned.

## Deferred Queued Message V1

Phase 1 intentionally has no busy-worker queue. A message sent to an active worker returns `accepted: false`.

If a later phase proves queued delivery is needed, use a bounded shape such as:

```json
{
  "message_id": "msg_...",
  "text": "Use the existing state model; do not add a database.",
  "from_worker_id": null,
  "created_at": "timestamp"
}
```

Rules:

- cap message length;
- cap queue length;
- remove a message after successful dispatch;
- do not persist duplicate full conversation logs.

## Job Record Extensions

Worker extension:

```text
options._worker_id: optional string
options._worker_name: optional string
options._worker_workspace_mode: optional string
options._worker_base_repo_path: optional private path
options._worker_worktree_path: optional private path
options._worker_branch_name: optional string
options._worker_base_revision: optional git sha
options._worker_workspace_discarded: optional boolean
```

Later minimal extensions may add:

```text
worktree_owner: "job" | "worker" | "none"
```

Worker-owned worktrees must not be deleted by ordinary job cleanup.

Old persisted jobs without these fields must load with safe defaults.

## Public Worker View

Never return the complete private worker record directly.

Public summaries may include:

```json
{
  "worker_id": "wrk_...",
  "name": "Connector Investigator",
  "workspace_id": "ws_...",
  "workspace_name": "patchbay",
  "state": "idle",
  "report": "...",
  "has_session": true,
  "can_message": true,
  "last_activity_at": 1234567890.0
}
```

## Restart Semantics

On server start:

1. load durable jobs as today;
2. active jobs that did not finish before shutdown are marked failed by the existing loader;
3. worker lists/inspections derive current state from the loaded worker-tagged jobs.
4. preserve worker session references;
5. do not automatically restart arbitrary work in V1.

## Cleanup Semantics

- ordinary job cleanup must not delete worker-tagged durable jobs in Phase 1;
- stopping a worker cancels only the active turn and preserves worker history;
- future explicit cleanup should remove worker worktrees only after checking no active job uses them;
- rejected alternatives should remain available until explicit cleanup.
