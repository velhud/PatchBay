# Hub Manager Tool Contract

Design ID: `HUB-MANAGER-CONTROL-PLANE-V2`

Status: implemented contract; exact registry authority is `patchbay.hub.tool_surface`.

## Design Rule

The public Hub catalog must express managerial actions, not transport commands.
The manager chooses a group and worker; Hub resolves machine, Edge action,
delivery, retries, and result routing.

The target catalog contains 31 tools. The number is not a target by itself. It
is the result of preserving meaningful capabilities, combining genuine
duplicates, removing irrelevant controls, and adding Hub/group/batch behavior.

## Common Target Envelope

Most routed tools accept one of these targets:

```text
Preferred grouped target:
  work_group_id
  lane when starting a worker

Worker target:
  work_group_id plus worker name or fleet_worker_ref

Exceptional ungrouped target:
  machine_id
  workspace_ref or repo_path
  ungrouped_reason = tiny_check | operator_requested | legacy_compat
```

Callers should not repeatedly supply `machine_id` for a grouped worker. Hub
resolves the owning machine from immutable group/worker records.

Every mutating public call requires a caller-generated `idempotency_key`.
Generate it before the first call and reuse it only when retrying the exact same
semantic payload. Hub never invents a missing key. A same-key, same-payload
retry returns the existing operation/result; a same-key, different-payload call
returns `blocked` with `idempotency_payload_conflict`.

## Common Semantic Output

Every tool defines a strict action-specific output schema inside this envelope:

```json
{
  "status": "ok|pending|partial|blocked|failed|not_found",
  "result": {
    "summary": "Action-specific semantic result",
    "work_group": {},
    "lane": {},
    "worker": {},
    "machine": {},
    "workspace": {}
  },
  "operation": {},
  "warnings": [],
  "next_actions": []
}
```

The five top-level fields are always present. Action-specific objects live under
`result`; omit irrelevant result members without changing status meaning.
`pending` means the domain result is not known yet. Hub queue acceptance alone
never produces `ok`. `needs_confirmation` and `unknown_outcome` are internal or
domain reasons represented inside a canonical public status, not extra public
status values.

## Family 1: Fleet And Discovery (2)

### 1. `patchbay_fleet_status`

Purpose: one compact operational view of Hub and current usable machines.

Inputs:

- `include_offline` (default true);
- `include_retired` (default false, audit only);
- optional `query` and `tags`;
- optional `include_workspaces` compact summary;
- optional `since_revision`.

Returns:

- Hub/manifest/schema version;
- routing enabled/disabled;
- online/offline/incompatible machine counts;
- each visible machine's capacity, resource pressure, worker slots, queue
  policy, heartbeat freshness, Edge protocol/tool-contract version, and compact
  workspace summary;
- current conversation group and owned active-group summary;
- recovery warnings and next actions.

Replaces old self-test/config checks for ordinary operation. It reports live
facts rather than a generic readiness label.

### 2. `patchbay_workspace_list`

Purpose: find logical repositories/workspaces across eligible machines without
guessing absolute paths.

Inputs:

- `query`;
- `discover`;
- `machine_ids` and `required_tags` filters;
- `include_offline`;
- `max_depth` and `max_results` transport bounds.

Returns logical `workspace_ref` records, aliases, repository identity when
known, machine availability, and whether the workspace is ready, stale, or
requires preflight. It replaces old `codex_list_workspaces` and current
`patchbay_machine_workspaces`.

## Family 2: Work Groups (6)

### 3. `patchbay_work_group_create`

Purpose: create one durable non-trivial task, choose/pin one machine generation,
and perform strict preflight.

Inputs:

- required `title`, `goal`;
- preferred `workspace_ref`, optional `repo_path` compatibility hint;
- optional explicit `machine_id`;
- optional `allowed_machine_ids`, `required_tags`;
- optional initial lane objects `{lane, title, role}`;
- `visibility = private|shared`;
- `idempotency_key`;
- optional bounded `wait_for_preflight_seconds`.

Behavior:

- availability-only routing when machine is omitted;
- returns selection reasons and candidate/rejection summaries;
- pins one immutable machine generation;
- waits briefly for preflight;
- returns `succeeded` only when group exists; readiness is separately reported;
- duplicate retry returns the same group.

### 4. `patchbay_work_group_list`

Purpose: discover current, owned, recent, or historical groups without dumping
all history.

Inputs:

- `scope = current|owned|recent|history`;
- optional `status`, `workspace_ref`, `machine_id`, `query`;
- `include_closed`;
- pagination cursor/limit.

Default: current conversation group plus owned open groups. Return hidden counts.

### 5. `patchbay_work_group_status`

Purpose: authoritative group overview.

Inputs:

- `work_group_id` or current group;
- optional `since_revision`;
- optional `wait_for_change_seconds` bounded by server policy;
- optional compact `include_workers`, `include_operations`, and
  `include_integrations` flags.

Returns:

- persistent lifecycle, readiness, activity, outcome;
- pinned machine generation and workspace proof;
- lanes derived from actual workers;
- active/quiet/stale/lost/completed/failed workers;
- queued/leased/uncertain operations;
- pending integration and cleanup disposition;
- latest compact reports/checkpoints;
- next recommended manager action.

It never treats command completion as worker completion.

### 6. `patchbay_work_group_resume`

Purpose: make one owned open group current for this ChatGPT conversation.

Inputs:

- required `work_group_id`;
- optional `takeover`, `takeover_reason` for another active participant;
- optional preflight refresh wait.

Behavior:

- closed groups cannot be reopened;
- same stable owner can discover owned groups across conversations;
- participant/session ownership is recorded;
- stale machine/workspace/worker projections are refreshed;
- returns the group and exact continuation state.

### 7. `patchbay_work_group_reassign`

Purpose: create successor work on another machine when the original machine is
unavailable or the operator explicitly requests a move.

Inputs:

- required `work_group_id`, `reason`;
- optional explicit `machine_id`, allow-list, tags;
- optional `carry_context = reports|reports_and_changes|none`;
- `idempotency_key`.

Behavior:

- never changes the original group's machine;
- cancels safe unclaimed operations on the predecessor;
- preserves claimed/uncertain operations for reconciliation;
- creates and returns a linked successor group;
- re-resolves the logical workspace on the successor Edge;
- does not claim to migrate live sessions, worktrees, or artifacts.

### 8. `patchbay_work_group_close`

Purpose: close one group with an explicit outcome and durable manager summary.

Inputs:

- required `work_group_id`;
- `outcome = complete|partial|abandoned|failed`;
- required `summary`;
- required `worker_dispositions`, one explicit disposition for every worker in
  the group: `integrated`, `no_changes`, `reviewed_failure`,
  `stopped_preserved`, `discarded`, or `leave_running`;
- `discarded` requires explicit `discard_unintegrated_changes=true` for that
  worker;
- `idempotency_key`.

Rules:

- `complete` refuses active, uncertain, failed-unreviewed, or unintegrated work;
- group close records the manager decision only: it never stops a worker or
  cleans/disposes a workspace;
- explicitly stop active workers with `patchbay_worker_stop` before closing when
  they are not intentionally retained, and complete any workspace disposal with
  that worker-specific control before recording the close;
- `leave_running` records the exceptional retained-worker decision only. It
  performs no stop or cleanup and cannot produce a complete outcome;
- closing cancels safe unclaimed group operations;
- closing never rewrites an active worker as idle;
- closed group history remains immutable.

## Family 3: Workers And Artifacts (11)

All existing mature worker fields are preserved. Hub adds group/lane/fleet
routing; it does not replace worker behavior.

### 9. `patchbay_worker_options`

Purpose: return actual model/reasoning options from the group's pinned Edge or
an explicit machine.

Inputs:

- `work_group_id` or `machine_id`;
- optional `model`, `max_models`, `include_model_details`;
- harmless compatibility `repo_path` may be ignored.

Returns the Edge Codex version, defaults, models, reasoning efforts, and advisory
selection guidance. This is read-only even though it travels to Edge.

### 10. `patchbay_worker_inbox`

Purpose: import, list, inspect, and clean ChatGPT-supplied artifacts on the
group's Edge.

Preserved fields:

- `action = import_file|list|inspect|cleanup`;
- Apps `artifact_file` parameter;
- `artifact_id`, `label`;
- `work_group_id` or explicit machine/workspace target;
- `view = summary|tree|file|raw_manifest`;
- `file_path`, `max_bytes`, `max_entries`;
- `takeover`, `takeover_reason`;
- `idempotency_key` for mutations.

Returns machine-qualified artifact refs. Artifacts remain Edge-affine.

### 11. `patchbay_worker_start`

Purpose: appoint one durable named Codex worker.

Required:

- `work_group_id`, `lane`, `name`, `brief` for normal work.

Preserved optional fields:

- `repo_path` may be omitted or repeat the exact repository resolved by the
  group preflight; it cannot retarget grouped work to another repository;
- `workspace_mode = isolated_write|read_only|shared_write`;
- `auto_suffix`;
- `include_untracked_from_base` patterns;
- `context_from_workers`;
- `context_from_artifacts`;
- `context_detail = report|changes|diff|review`;
- `model`, `reasoning_effort`;
- `idempotency_key`.

Exceptional ungrouped starts require explicit machine/workspace and
`ungrouped_reason`.

Returns the actual worker record when Edge accepts it, including stable
`fleet_worker_ref`, state, model, worktree mode, machine, group, lane, and
message/status capabilities.

### 12. `patchbay_worker_start_batch`

Purpose: appoint a parallel team in one call with shared task context and
individual missions.

Inputs:

- required `work_group_id`;
- `shared_brief` containing common goal/context/constraints;
- shared `context_from_workers`, `context_from_artifacts`, `context_detail`;
- `workers[]`, each containing:
  - required stable `item_id` and child `idempotency_key`;
  - required `name`, `lane`, `mission`;
  - optional `workspace_mode`, `model`, `reasoning_effort`;
  - optional personal context/artifacts;
  - optional `include_untracked_from_base`, `auto_suffix`;
- `idempotency_key`.

Behavior:

- validate the whole batch before dispatch;
- every worker receives `shared_brief + personal mission`;
- all workers route to the group's pinned Edge;
- start independently up to capacity; queue on that Edge according to policy;
- return one item result per worker;
- partial start does not roll back already created workers;
- retry cannot duplicate successful items;
- no artificial small worker count below Edge/configured capacity.

Group-level operations that exist before any worker, especially repository
preflight, recommend a bounded `patchbay_work_group_status` wait. They never
recommend `patchbay_worker_wait` until at least one worker turn is active.

### 13. `patchbay_worker_message`

Purpose: continue the same worker through natural language.

Preserved fields:

- `work_group_id`, `worker` name or `fleet_worker_ref`;
- required `message`;
- optional `context_from_workers`, `context_from_artifacts`, `context_detail`;
- optional `model`, `reasoning_effort` override;
- `takeover`, `takeover_reason`;
- `idempotency_key`.

Behavior:

- resolve immutable owning Edge/workspace;
- preserve Codex session and worktree continuity;
- start the next Codex turn only after the current turn is terminal;
- return `active_turn_in_progress` while a turn is still active rather than
  claiming active steering or queued delivery;
- report whether the continuation started, was blocked, or needs confirmation.

### 14. `patchbay_worker_list`

Purpose: discover/reuse workers without historical clutter.

Inputs:

- required `work_group_id`;
- optional `lane`, `active_only`, `include_stopped`;
- pagination.

Hub list/status/wait are only work-group views. They do not accept
single-machine `repo_path`, ownership/history `scope`, `owned_only`, or
`created_after` filters because those filters have no Hub projection meaning.

Returns stable worker refs, compact state, latest report summary, machine,
lane, integration/disposition, and freshness.

### 15. `patchbay_worker_status`

Purpose: compact team status from authoritative Hub projections.

Inputs mirror list filters plus optional `since_revision`. There is no
`force_refresh` compatibility field: Hub enforces the manager/group monitoring
cadence.

Returns activity deltas, liveness counts, one line per worker, checkpoints,
recommended next poll interval, projection freshness, and suggested action. It
does not return fleet/group status under the same name. List and status share a
20-second minimum / 30-second recommended cache per manager and group; an early
repeat returns cached `poll_too_early` guidance.

### 16. `patchbay_worker_wait`

Purpose: wait once for worker/group projection change, then return worker status.

Inputs:

- worker status filters;
- `wait_seconds`;
- optional `since_revision`.

Hub waits on its event/projection store. It does not queue a sleeping Edge
command and does not block Edge heartbeat/message/stop processing. Omitted
waits use 30 seconds; values below 20 seconds are raised to 20.

### 17. `patchbay_worker_inspect`

Purpose: read one worker's report or focused evidence.

Preserved fields:

- `work_group_id`, `worker`;
- optional `wait_seconds`;
- `view = report|compact|status|diagnostics|changes|diff|file|integration_preview`;
- `file_path`, `start_line`, `end_line`, `max_bytes`;
- `accepted_dirty_base` patterns.

Returns the actual Edge inspection result with pagination and freshness. It is
read-only for every view.

`integration_preview` additionally returns a short-lived `preview_token` bound
to worker revision, patch hash, base revision, and target workspace.

### 18. `patchbay_worker_integrate`

Purpose: apply one explicitly accepted isolated worker result to its owning base
checkout without committing.

Inputs:

- `work_group_id`, `worker`;
- required `preview_token` in Hub mode;
- `allow_dirty_base`, `accepted_dirty_base`;
- `takeover`, `takeover_reason`;
- `idempotency_key`.

Returns `applied`, blocked/conflict files, base/patch revisions, and exact side
effects. It is destructive and non-idempotent at the domain layer but protected
by the operation/preview idempotency contract.

### 19. `patchbay_worker_stop`

Purpose: interrupt an active worker turn and optionally clean its isolated
workspace.

Preserved fields:

- `work_group_id`, `worker`;
- `cleanup_workspace`;
- `force`;
- `takeover`, `takeover_reason`;
- `idempotency_key`.

Stop preserves captured reports/checkpoints. Cleanup is explicit and cannot
silently discard unintegrated changes. A completed/idle worker remains a durable
group record and is hidden through scopes rather than deleted casually.

## Family 4: Exceptional Manager Workspace Inspection (5)

These tools are allowed but not the default broad-work loop. They route through
the group pin or explicit machine/workspace target and preserve Edge path guards.

### 20. `patchbay_workspace_open`

Preserves old open-workspace fields: tree inclusion/depth/entries, hidden files,
and bounded instruction summary. Skill listing is not exposed as a separate Hub
manager capability.

### 21. `patchbay_workspace_tree`

Preserves `path`, depth, entry, and hidden controls.

### 22. `patchbay_workspace_search`

Preserves query/path/glob/regex/hidden/result/timeout controls and structured
partial timeout recovery.

### 23. `patchbay_workspace_read_file`

Preserves file path, line range, response-page `max_bytes`, and next-page
locators. It reads the base checkout; worker-created files use worker inspect.

### 24. `patchbay_workspace_changes`

Combines the genuinely overlapping old git tools.

Inputs:

- `view = status|summary|diff`;
- `work_group_id` or explicit machine/workspace;
- optional `file_path`, `staged`, `include_diff`, `max_bytes`;
- `porcelain` for status view.

Returns branch/status/change inventory/stats/diff according to the strict view
schema.

## Family 5: Pro Requests (6)

These preserve the current separation because storing a response and dispatching
it to Codex are different side effects.

### 25. `patchbay_pro_request_list`

Inputs: group/machine/workspace target, status, limit, include closed. Returns
machine-qualified request refs and compact metadata.

### 26. `patchbay_pro_request_read`

Preserves report/response/event inclusion and report/response byte bounds.
Applies group/owner visibility and repository staleness checks.

### 27. `patchbay_pro_request_claim`

Preserves note, takeover, and takeover reason. Adds claim revision/lease so two
ChatGPT conversations cannot silently answer the same request.

### 28. `patchbay_pro_request_respond`

Preserves response kind, response Markdown, recommended next action, worker
message Markdown, and takeover. This stores only; it does not dispatch, edit,
integrate, commit, or execute.

### 29. `patchbay_pro_request_dispatch`

Preserves target, message source, new worker name, workspace mode, and takeover.
This is the explicit execution boundary and returns a routed worker operation.

### 30. `patchbay_pro_request_close`

Preserves final status, reason, and takeover. It closes/supersedes request state
without pretending worker work was integrated.

## Family 6: Exceptional Operation Recovery (1)

### 31. `patchbay_operation_status`

Purpose: recover a routed call that returned pending or unknown outcome.

Inputs:

- required `operation_id`;
- optional bounded `wait_seconds`;
- optional `include_result`;
- optional `since_revision`.

Returns dispatch state, semantic operation outcome, attempt/lease metadata,
Edge receipt/reconciliation state, actual domain result when available, and the
safe next action.

This tool is not part of the ordinary worker loop and does not expose raw queue
arguments or results belonging to another owner/group.

## Old 31-Tool Disposition

| Old capability | Target disposition |
| --- | --- |
| `codex_open_workspace` | `patchbay_workspace_open` |
| `codex_repo_tree` | `patchbay_workspace_tree` |
| `codex_read_file` | `patchbay_workspace_read_file` |
| `codex_search_repo` | `patchbay_workspace_search` |
| `codex_list_workspaces` | `patchbay_workspace_list` |
| `codex_git_status`, `codex_git_diff`, `codex_show_changes` | combined in `patchbay_workspace_changes` |
| ten `codex_worker_*` tools | ten full-parity `patchbay_worker_*` tools |
| six `codex_pro_request_*` tools | six machine/group-aware equivalents |
| `codex_load_context` | omitted: group/workspace orientation, artifacts, peer context, and natural worker briefs own this job |
| `codex_list_skills`, `codex_load_skill` | omitted: Edge Codex workers discover/use skills; manager does not administer them |
| `codex_self_test`, `codex_get_config` | omitted: `patchbay_fleet_status` reports current health/capability versions |
| `codex_tool_mode_info`, `codex_tool_mode_switch` | omitted: Hub exposes one stable manager catalog |
| new Hub group/fleet/recovery needs | eight group/fleet/operation tools added |
| missing batch team appointment | `patchbay_worker_start_batch` added |

## Tools Explicitly Not Exposed

- no `patchbay_worker_start_auto`: group creation already performs routing;
- no separate machine list/workspaces/recommend trio: fleet status and workspace
  list cover discovery, while group creation returns routing reasons;
- no public command queue tool: `operation_status` is the exceptional semantic
  recovery surface;
- no direct write/edit/bash tools in manager mode: delegate such work to Codex
  workers;
- no lane CRUD tools: lanes are lightweight group labels created by worker
  appointment;
- no worker delete tool: stop/cleanup and group scopes preserve evidence;
- no campaign/channel tools until their durable coordination model exists.

## Manifest And Descriptor Tests

The build must fail if:

- the count or canonical list changes without an explicit contract update;
- a mapped canonical field disappears;
- a read-only tool is marked mutating or vice versa;
- destructive tools omit destructive annotations;
- input/output schemas allow unknown fields unexpectedly;
- a tool result violates its semantic envelope;
- Hub and Edge contract hashes are incompatible without preflight blocking.
