# Public tool surface

PatchBay presents separate public surfaces for manager/worker mode and Author Mode. Their implementation authority must remain distinct.

## Single-machine manager surface

The ChatGPT-facing `worker` mode combines durable worker lifecycle tools with bounded read-only workspace context.

### Orientation and workspace tools

- `codex_self_test`
- `codex_open_workspace`
- `codex_list_workspaces`
- `codex_repo_tree`
- `codex_search_repo`
- `codex_read_file`
- `codex_git_status`
- `codex_git_diff`
- `codex_show_changes`
- AGENTS, skills, and context-loading tools

These are manager inspection instruments. They are not a mandate for ChatGPT to perform broad repository implementation manually.

### Durable worker tools

- `codex_worker_options`
- `codex_worker_inbox`
- `codex_worker_start`
- `codex_worker_message`
- `codex_worker_list`
- `codex_worker_status`
- `codex_worker_wait`
- `codex_worker_inspect`
- `codex_worker_integrate`
- `codex_worker_stop`

Writing workers use isolated worktrees by default. Integration is explicit, preview-bound, no-commit, and preserves the worker worktree.

### Pro Request tools

- list/read/claim
- respond
- dispatch
- close

`respond` stores a response. `dispatch` is the explicit boundary that sends it to an origin worker or starts a new worker. Neither tool implies hidden application, commit, automatic model routing, or prompt-authority escalation.

### Power and compatibility tools

`standard`, `full`, and `minimal` modes can expose additional job-control, direct-write, bash, transcript, or compatibility aliases. Aliases resolve to canonical handlers and do not create a safer or separate authority path. Full mode does not change the manager-first posture.

## Current worker model contract

`codex_worker_options` and `patchbay_worker_options` return the installed catalog and supported reasoning efforts. The ordinary policy is explicit Luna/High when supported:

```text
model = gpt-6-luna
reasoning_effort = high
```

Every delegated worker MUST explicitly use model='gpt-6-luna' with reasoning_effort='high' by default; xhigh or max is allowed. No other model family, older Luna, ambient default, fallback, or escalation is permitted. If Luna is unavailable, report the blocker. Apply this same rule to nested helpers and every handoff. The primary manager is separate from delegated workers; Author Mode has no delegated author or recovery worker.

## Hub V2 manager surface

Hub V2 is the default multi-machine control plane and exposes the complete `patchbay_*` manager catalog. The source schema in `patchbay.hub.tool_surface` is authoritative.

Tool families:

- fleet and workspace discovery;
- durable work-group create/list/status/resume/reassign/close;
- worker options/inbox/start/batch/message/list/status/wait/inspect/integrate/stop;
- focused workspace inspection;
- Pro Requests;
- operation status and recovery projection.

One task is one durable group. Every worker in the group remains pinned to one machine unless explicitly reassigned. Mutating calls use stable idempotency keys. `pending` means accepted for durable execution, not completed.

Fleet status is bounded orientation, not complete history or routing state. Focused tools remain authoritative for details, while internal routing evaluates the complete projection set.

## Author Mode interface

Author Mode is not exposed through worker tools. Its public interfaces are:

- `patchbay author ...`;
- `patchbay-author ...`;
- the Author HTTP/JSON-RPC/MCP interface implemented under `patchbay.authoring.interfaces`.

Roles are explicit (`operator`, `author`, `reader`). Qualification output omits the private instance ID. Approval requires an actor. Author operations use independent state and do not initialize manager workers.

Core capabilities include repository registration, task creation/listing, leases, deterministic context export, package application, checks, operation status/cancel/recovery, feedback/diff/logs, approval, publication/listing, integration tasks, backup/restore/GC/doctor, tokens, qualification, and HTTP serving.

`author_recover_task` accepts `operation_limit`, a strict integer from 1 to 200
with a default of 20 recent operations. Active and uncertain operations have a
separate counted section even when they predate that history window. Respect
all partial flags and use `author_recent_operations` cursor pages for complete
history. The domain/CLI recovery default remains 50. Full text plus structured
content remains the default MCP output; `structured-summary-v2` requires its
own observed client qualification and keeps full payloads in `structuredContent`.

## Result and safety contracts

- JSON-RPC requests are validated before dispatch.
- Paths remain inside declared roots; symlink and traversal escapes fail closed.
- Result bodies, logs, file pages, and public status are bounded.
- Unknown execution ownership remains fenced.
- Stale acceptance cannot authorize a changed candidate.
- Cleanup preserves undelivered content.
- Cross-process mutations are serialized where required.
- Signal delivery targets proven process ownership rather than recycled identifiers.
- CLI terminal failure states return non-zero status.

Exact schemas live in source and tests. A complete canonical Hub capture must
retain all 31 tools; a legitimate scoped or host-loaded subset is a separate
observation. Do not weaken annotations to match an incomplete client cache.
The [MCP interface contract](mcp-interface-contract.md) specifies safe selector
errors, supported versions, bounded text decisions, post-effect fault recovery
and the complete Author effect/annotation table. Full evidence remains structured
or paged; source publication does not establish native-client qualification.
