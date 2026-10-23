# Public tool surface

Author recovery distinguishes `candidate.source_context_id` (authoring
provenance, also exposed through deprecated `candidate.context_id`) from
`candidate.candidate_context_id` (an existing context for the exact current
revision and commit, otherwise null). Passing nonpublishing tasks return
`awaiting_review`, with external review rather than publication approval.
See the [trial follow-up contract and handoff](../repairs/2026-10-22-author-trial-follow-up.md).

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

The existing inspect tool accepts `candidate_id` for retained changes/file/diff reads. Integration accepts optional target-bound `review_decision`; start accepts `source_candidate` for dependent source. Hub group create/close optionally retain `source_contract`/`combined_verification`. The canonical tool count is unchanged. Safe stop/cleanup forwards discard false; Edge still refuses undelivered or uncertain removal.

Local start/message tools accept an optional `idempotency_key` (1–200 valid
UTF-8 characters). Exact keyed retries read the retained admission without
scheduling a second turn; changed arguments conflict. Keys are scoped to method
and configured owner identity. Unkeyed calls retain legacy behavior and the
conservative non-idempotent annotation. An active worker message is refused,
not queued. Local keyed integration requires its signed preview token as well
as the key. Hub mutation keys remain required under the Hub contract.

Local wait is read-first and can return early for terminal/actionable work or
meaningful status changes. `wait_outcome` is `actionable`, `status_changed` or
`timeout`; elapsed wait is independent of requested maximum budget. The default
idle cap is 30 seconds and must leave delivery margin on the selected route.
Hub waits share bounded per-group observation while retaining separate caller
filters/cursors. A future revision returns the full snapshot with
`resync_required=true`. `projection_observed_at` describes Hub snapshot
construction, not native progress; `observation_refresh_seconds` describes
internal sampling, not permission to poll faster. No new tools or compact
output default are introduced. See the [S03 contract](../repairs/2026-10-22-s03-observation.md).

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

`author_recent_operations` defaults to `traversal="strict"`: same-task changes
invalidate its cursor. `traversal="snapshot"` pins initial operation identities
and permits complete traversal during new activity; states stay live per page.
Carry both cursor and offset, respect partial flags, then recover current
authority before writing. This is not a durable snapshot across restore/pruning.

Author's existing `author_read_file` also supports exact base64 byte pages via
`byte_offset` (maximum 65,536 decoded bytes). Select one context or exact draft
version, including unsealed versions; follow byte continuation without switching
sources. Export selection retains complete presence metadata and exposes
`identity_only` explicitly. No tool was added: Author still has 11/20/22 tools
by qualified role. The [S05 contract](../repairs/2026-10-22-s05-author-custody.md)
describes changed schemas and separate client qualification.

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

## Native evidence and complete report source

The existing local and Hub worker inspect tools accept `view="report_source"`,
`report_source_sha256`, `report_offset` and `max_bytes`. Resolve the same worker,
follow returned `next_offset` while `has_more=true`, and preserve raw versus
returned source hashes and privacy filtering. Truncated previews expose their
original sizes, retained ranges and complete-source availability. No tool was
added; canonical Hub still has 31 tools.

Native identity conflicts block messaging and integration preview. Public
launch provenance distinguishes requested model/effort from unknown observed
values, and connector/catalog availability from static/native/workflow/client
qualification. The adapter is headless; see the
[S04 contract](../repairs/2026-10-22-s04-native-execution.md).

Inbox imports use bounded writer admission and an overall deadline. Inspect
before retry after timeout/loss; a cancelled caller does not prove writer exit.
Wait observations distinguish first/new completion from an unchanged mixed team
for each caller and selection. Local Pro Request dispatch remains non-idempotent
and must never be automatically retried. Author qualifications coexist by exact
principal/role/profile/release binding without widening grants. See the
[postimplementation contract](../repairs/2026-10-23-postimplementation.md).

Shared-write peers can supply reports to starts and follow-ups at every context
level. They cannot supply an isolated candidate or attributable patch: their
checkout may include other authors' changes. The peer handoff states that limit
rather than failing while looking for a nonexistent delivery baseline. Use an
isolated source worker for retained, target-bound review. See the
[live native-client repair](../repairs/2026-10-23-live-native-client.md).
