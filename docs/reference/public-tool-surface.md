# Public Tool Surface

## Design Principle

PatchBay should expose tools as product capabilities, not implementation conveniences. ChatGPT should see narrow, intentional tools that explain when to use them and what control boundary they cross.

The primary ChatGPT posture is lead/manager/consultant, not line-by-line repository implementer, primary repository file reader, default code reviewer, or file-level investigator. The worker-first surface should make ChatGPT ask "Which worker or worker team should I appoint?" for non-trivial repository, Documents, codebase, architecture, audit, debugging, implementation, or review work. Direct context tools remain available and useful for orientation, briefing context, focused checks, concrete escalation, specific doubts, and tiny tasks, but they should not become the main broad-work execution loop or routine diff-review loop.

Trust worker reports by default as competent employee reports. Managerial review means reading reports, asking follow-up questions, comparing stated outcomes with assigned goals, and deciding the next assignment. It does not mean routinely reading changed files, inspecting diffs, or redoing implementation detail yourself.

Delegation is a positive behavior. Tool descriptions and initialize instructions should make it natural for ChatGPT to create multiple named workers when work can be split cleanly. A 10-slot worker configuration should be treated as an opportunity to run investigators, implementers, reviewers, verification workers, and synthesis workers in parallel, not as a limit ChatGPT should avoid approaching for broad tasks.

Runtime authority should be visible without turning ChatGPT into the default implementer. When `codex_self_test` and the visible catalog show a dedicated full-access workbench/VM with full bash, direct write, broad allowed roots, and `danger-full-access`, ChatGPT should understand that local dependency setup, repo-local virtual environments, verification commands, generated artifacts, commits, and authorized private-repo pushes are normal engineering actions for an end-to-end task. Missing packages should normally lead to environment setup rather than weaker verification. The exception boundary is external/public/production/paid/credential-changing/irreversible work, where ChatGPT should ask before acting.

Repeated direct `codex_read_file`, `codex_search_repo`, `codex_git_diff`, or `codex_show_changes` calls are a negative signal for non-trivial work. They should push ChatGPT back to the manager posture: start or continue a worker, ask it the evidence question, and use direct inspection only after concrete escalation triggers such as contradiction, missing evidence after follow-up, failure, risk, worker request, or user-requested inspection.

The manager posture is stateful. ChatGPT should treat named workers as continuing specialists, not disposable one-shot summaries. For consequential work, prompts should ask workers for durable evidence such as report files, changed files, diffs, validation notes, or open-question lists. When a report is thin, contradictory, missing evidence, or decision-critical, ChatGPT should continue the same worker with `codex_worker_message` and use `context_from_workers` for synthesis or cross-review instead of manually copying summaries.

Worker status must make long-running work legible without turning the manager view into a historical archive. Running workers expose bounded `codex_worker_status` team summaries, one-line `status_line` values, `activity_since_last_check` deltas, `liveness`, `latest_partial_note`, `latest_checkpoints`, `checkpoint_count`, and `report_artifacts` so ChatGPT can see that a worker is alive without reading raw logs. Default `scope=current` shows the current work run plus live/problem workers and returns a hidden-history count; `scope=conversation`, `scope=recent`, and `scope=history` are explicit opt-ins for broader continuity/archive views. Compact status intentionally omits raw shell command text; `codex_worker_inspect(view="status")` is the single-worker liveness/diagnostic view, and `view="diagnostics"` is the explicit full lifecycle debugging view with `latest_turn` internals. A missing final report is not by itself a failure signal. ChatGPT should inspect the compact status bar/checkpoints before stopping or replacing a worker, then wait about 10-20 seconds before the next normal monitoring pull instead of polling every few seconds. When `repo_path` is omitted, worker list/status/wait should cover all allowed repositories so a manager cannot accidentally miss active work because the server default workspace is different from the task repo; callers can still pass `repo_path` to narrow deliberately. `codex_worker_status`, `codex_worker_list`, `codex_worker_wait`, and `codex_worker_inspect` status/compact/running-report views share a friendly cooldown and can return cached `poll_too_early` responses; that is not a tool failure and does not block worker start/message/stop/integrate or exact file/diff/integration inspection. `codex_worker_wait` is the patient path and raises too-small waits to the configured minimum cadence rather than enabling rapid polling. Liveness freshness and quiet windows are display policy, configurable under `workers.heartbeat_fresh_seconds` and `workers.heartbeat_quiet_seconds`, not hard task limits; status polling guidance is separately configurable under `workers.status_recommended_poll_seconds` and `workers.status_minimum_poll_seconds`. Stopping a worker preserves any captured partial report/checkpoints, and the stop response can briefly wait for captured evidence to attach through `workers.stop_artifact_wait_seconds`, but it is still an interruption. `workers.stop_confirmation_grace_seconds` adds a manager confirmation gate for live or recently started workers: the first stop can return `stop_confirmation_required` instead of cancelling, and ChatGPT must either wait or deliberately retry with `force=true`. Completed worker results should expose a manager-grade report, not only a short summary: the Codex structured output schema asks for `summary`, `detailed_report`, `evidence`, changed files, commands/tests, notes, risks, open questions, and next steps, and the public worker report surfaces those fields when present. If Codex does not emit the final structured schema, PatchBay exposes a fallback artifact from the latest agent message or bounded raw-output preview, with result-source metadata showing whether a Codex result event, turn completion, and parsed output schema were seen.

Workspace discovery should prevent path guessing. `codex_list_workspaces` lists configured roots and, when `repositories.discovery_roots` is set, shallowly discovers likely repositories under those roots. If ChatGPT knows a repository name but not its exact path, it should call `codex_list_workspaces(query=..., discover=true)` and pass the returned `root` as `repo_path`, not try many guessed absolute paths. `codex_search_repo` still supports broad searches, but timeout is a structured partial result with `timed_out`, `timeout_ms`, `searched_path`, and `suggested_next`; that is a recovery signal to narrow the search, intentionally raise the timeout, or delegate broad search to a worker.

The same public tool surface is served through Streamable HTTP `/mcp` and the stdio entry point (`patchbay stdio` / `patchbay-stdio`). Stdio is a transport compatibility layer; it must not fork tool policy, hidden-tool filtering, schema validation, or session-local tool mode behavior.

Generic `read`, `write`, `edit`, and `bash` aliases are powerful. PatchBay keeps canonical `codex_*` names as the durable API, while `app.tool_mode` can advertise compatibility aliases for ChatGPT live use. Aliases are tool-selection aids, not separate or safer execution paths; they resolve to canonical handlers and use precise alias-specific schemas instead of open generic argument bags. Full mode and aliases do not change ChatGPT's manager-first role; they only make exceptional controls available when worker-mode controls are insufficient.

## Optional Hub Tool Surface

Hub mode is a separate optional MCP server. It does not expose every direct
single-machine `codex_*` tool from every edge machine. It exposes a smaller
fleet-native manager surface and queues commands to the selected edge machine:

| Tool | Role |
| --- | --- |
| `patchbay_fleet_status` | Compact status of online/offline machines and visible worker projections. |
| `patchbay_machine_list` | List enrolled machines, tags, capabilities, and safe workspace projections. |
| `patchbay_machine_workspaces` | Show advertised workspaces on one machine or the whole fleet. |
| `patchbay_machine_recommend` | Recommend the least-busy eligible online machine when optional availability routing is enabled. |
| `patchbay_worker_options` | Route a model/reasoning options request to one machine. |
| `patchbay_worker_start` | Start a worker on a selected machine. |
| `patchbay_worker_start_auto` | Start a worker on the least-busy eligible online machine when optional availability routing is enabled. |
| `patchbay_worker_message` | Continue a worker on the same machine. |
| `patchbay_worker_status` | Show cached fleet worker status or queue a machine-local refresh. |
| `patchbay_worker_wait` | Queue a patient status refresh on one machine. |
| `patchbay_worker_inspect` | Inspect one worker through its machine-local PatchBay runtime. |
| `patchbay_worker_stop` | Stop one worker turn on the owning machine. |
| `patchbay_worker_integrate` | Apply an accepted isolated worker result on the owning machine. |
| `patchbay_command_status` | Inspect hub-routed command state. |

Hub initialize instructions must tell ChatGPT to behave as a fleet manager:
start with fleet status, choose machines by workspace/capability, use explicit
`machine_id`, and collect reports for cross-machine synthesis. Hub state is a
projection and command queue; edge machines keep local Codex auth, repositories,
worker state, worktrees, and authority policy.

The optional hub router is availability-only. It is off by default in public
config and, when enabled, compares current worker load, CPU pressure, memory
pressure, disk feasibility, online state, and explicit required tags. It must
not infer task type, complexity, model choice, repository meaning, or
documentation-vs-coding intent. ChatGPT should use auto-routing only when the
user has not named a machine; explicit `machine_id` selection remains the
normal override and is unchanged.

## Current Stable Tools

| Tool | Current role | Target status | Notes |
| --- | --- | --- | --- |
| `codex_plan_job` | Start Codex analysis using configured sandbox | keep | In the full-power profile this is open-world and not read-only; narrower profiles may use a read-only sandbox. |
| `codex_apply_job` | Start isolated worktree apply job | keep | Mutating. Should return worktree, branch, and review artifacts. |
| `codex_get_status` | Poll job state | keep | Read-only. Should work for durable jobs after restart. |
| `codex_get_result` | Fetch completed output | keep | Return summary by default, raw logs only opt-in. |
| `codex_get_diff` | Inspect file diff | keep | Requires completed apply job and changed file membership. |
| `codex_review` | Run Codex review | keep | Clarify whether it is read-only or can trigger writes through options. |
| `codex_list_sessions` | List metadata-only session ids | keep | Read-only merge of PatchBay-known job sessions and configured Codex-home session metadata; no transcript bodies, repo paths, or source paths. |
| `codex_resume` | Start async Codex resume job | keep, strengthen | Marked mutating/open-world because resumed sessions may write locally; returns a durable `job_id`. |
| `codex_interactive` | Start async interactive Codex exec job | keep, strengthen | Marked mutating/open-world; completed result includes `session_ref` when Codex reports one. |
| `codex_interactive_reply` | Start async Codex continuation job | keep, strengthen | Marked mutating/open-world; uses session repo metadata when available. |
| `codex_get_config` | Return redacted config/capabilities | keep | Does not expose raw local config, private paths, or hidden feature details. |

## Natural-Language Worker Tools

PatchBay includes durable natural-language workers summarized in [../worker-bridge/README.md](../worker-bridge/README.md). These tools are the preferred durable delegation path when ChatGPT wants to manage an ongoing named Codex colleague without exposing job ids, session ids, branch names, or private paths.

For larger tasks, ChatGPT may start a small team of workers with separate responsibilities such as investigation, backend implementation, UI implementation, tests, review, or integration risk. PatchBay does not impose fixed roles; ChatGPT chooses the management shape and passes bounded `report`, `changes`, `diff`, or `review` context between workers with `context_from_workers`. Use `review` for report plus changed-file inventory plus bounded diff when another worker must review a peer result before integration. Up to 10 peer workers can be attached to a worker start/message; for larger campaigns, use batches or a synthesis worker rather than manually copying transcripts.

| Tool | Mutability | Role |
| --- | --- | --- |
| `codex_worker_options` | read-only | Return a bounded setup menu for Codex worker model and reasoning choices loaded from the installed Codex runtime/catalog. |
| `codex_worker_inbox` | mutating/open-world/destructive/non-idempotent | Import ChatGPT-supplied files or zip packages into a local artifact inbox, list/inspect them, or remove local inbox copies. Import does not edit the repo. |
| `codex_worker_start` | mutating/open-world/non-idempotent | Create a named worker from a natural-language brief and optionally include bounded context from other workers. Defaults to `isolated_write`. |
| `codex_worker_message` | mutating/open-world/non-idempotent | Continue an existing worker by name or id using the prior Codex session and workspace; optionally include bounded context from other workers. |
| `codex_worker_list` | read-only | List scoped workers with bounded state, latest report, compact `team_status` / `team_report`, hidden-history count, and optional filters. |
| `codex_worker_status` | read-only | Return the compact pull-based current-scope team status bar: status counts, deltas since last check, suggested action, one short line per worker, hidden-history count, and the recommended next polling interval. |
| `codex_worker_wait` | read-only | Wait once, then return a fresh compact worker status so ChatGPT does not poll every few seconds while workers are active or quiet. |
| `codex_worker_inspect` | read-only | Return one worker's report, compact/status/diagnostics view, changed-file inventory, paged worker-created file content, one-file diff, or integration preview, optionally waiting briefly. |
| `codex_worker_integrate` | destructive/non-idempotent | Apply an explicitly accepted isolated writing worker result to the base checkout. Does not commit or delete the worker worktree. |
| `codex_worker_stop` | destructive/non-idempotent | Cancel only the active worker turn while preserving durable identity and prior session continuity; may require `force=true` confirmation for live or recently started workers; optionally discard an isolated workspace. |

Worker names are scoped to the base workspace. The same human name can be reused in another repo; worker ids remain globally addressable for explicit disambiguation, and `auto_suffix` can create a rerun name for repeated phases. Worker results omit low-level job ids, Codex session ids, absolute repo/worktree paths, branch names, raw transcripts, and raw process logs. Public report views show the worker answer plus manager status fields and intentionally omit `latest_turn`; `view=status` gives one-worker liveness/turn diagnostics without the completed answer as the main payload; `view=diagnostics` exposes the full bounded lifecycle payload for explicit debugging. Compact status omits the raw command preview. PatchBay streams Codex JSON events while the process is running, so `thread.started` records session creation before final completion and `agent_message` events can become manager-level checkpoints. `codex_worker_status` and `codex_worker_list.team_status` are lightweight coordination views and do not scan worker worktrees for exact change state; unscoped list/status/wait calls cover all allowed repositories, while `repo_path` intentionally narrows the view. Their default `scope=current` hides old completed/stopped workers and reports a hidden-history count; use `scope=conversation` for same-ChatGPT-conversation continuity, `scope=recent` for recently active workers, and `scope=history` for the full durable archive. Use `active_only`, `include_stopped=false`, `owned_only`, or `created_after` for additional narrowing, and use `codex_worker_inspect` compact/status/diagnostics, change, diff, file, or integration-preview views when exact worker state or changes matter. Monitoring views include `poll_too_early`, `status_current`, `seconds_since_last_poll`, and `retry_after_seconds`; too-early cached responses do not reset the activity delta baseline and should make ChatGPT wait, not abandon the task. Use `codex_worker_wait` for patient monitoring; values below the configured minimum cadence are raised to that minimum. Liveness status uses the compact categories `starting`, `active`, `quiet`, `stale`, `lost`, `completed`, `failed`, and `cancelled`. Terminal jobs clear live-only command fields, and PatchBay persists a redacted result artifact even when Codex did not emit the final structured result event. The fallback result uses the latest agent message when available or a bounded raw-output note/preview when no worker message was captured; `report_artifacts` include result-source metadata for this distinction. Persisted durable `running` jobs survive PatchBay restart as recovered-running records and are reconciled to `failed` only after the grace window and only when PatchBay has neither a live executor task, a tracked live subprocess, a live recorded process pid, nor a recent heartbeat for that job. A live process that never emits a Codex JSON session can fail by `codex_session_start_timeout_seconds` without imposing an overall limit on long-running turns when `job_timeout_seconds` is disabled. Worker identity and workspace ownership come from private durable job metadata; peer-worker context is bounded and explicit. `include_untracked_from_base` can copy selected accepted untracked base files into a new isolated worker. Unchanged copied baseline files are excluded from integration patches; modified copied baseline files appear in `modified_included_untracked_base_files` and block automatic apply so ChatGPT can ask for a separate patch, integrate manually, or commit/track the base context first. Accepted-result integration is explicit and does not commit; `accepted_dirty_base` can allow known phase artifacts while unexpected dirty files still block. Codex auth/session startup is serialized per effective Codex home with a process-local gate and host file lock, and spawned Codex CLI jobs receive that same home through `CODEX_HOME`. PatchBay can queue pending Codex turns behind `max_concurrent_jobs`, but it does not add a worker database, mailbox, queued worker-message delivery, active-turn steering surface, transcript copy, role engine, automatic reviewer chain, automatic commits, or a merge queue. Routine worker MCP results keep full data in `structuredContent` while the visible text `content` is compact, so long reports do not duplicate huge JSON into the ChatGPT interface.

Recommended ChatGPT worker-management loop:

1. Start with `codex_self_test` and `codex_open_workspace`.
2. Use read-only context tools only enough to understand the allowed workspace and constraints.
3. Start one or more named workers with outcome, context, constraints, deliverables, and report format.
4. For important writable work, ask workers to create a durable report file or changed-file evidence in the worker workspace; for read-only work, rely on PatchBay structured reports/checkpoints rather than source writes.
5. Use `codex_worker_status` while workers run, then wait about 10-20 seconds before the next normal monitoring check; use `codex_worker_wait` instead of rapid polling when workers are active or quiet; inspect worker reports, liveness/checkpoints, and exact changes when needed.
6. Continue the same worker with `codex_worker_message` when evidence is weak, contradictory, missing validation, or needs another worker's report.
7. Use `context_from_workers` for synthesis, review handoff, and cross-worker reconciliation.
8. Preview integration before applying accepted isolated-worker work.

Worker workspace modes:

- `isolated_write`: default; one external worker worktree reused across turns.
- `read_only`: advisory/review mode with a forced read-only Codex sandbox; reports, latest partial notes, and checkpoints are PatchBay-managed runtime artifacts, not files written into the source checkout.
- `shared_write`: explicit direct-workspace mode.

Worker execution options use progressive disclosure:

- `codex_worker_options` is the read-only menu tool. It can load the current Codex model catalog through `codex debug models` or the local Codex model cache, then returns only bounded public metadata plus advisory model-selection guidance.
- `codex_worker_options` is not repository-scoped. Do not pass `repo_path`; choose the model/reasoning menu, then pass selected values to worker start/message.
- `codex_worker_start` accepts optional `model` and `reasoning_effort`; omit them to use Codex defaults.
- `codex_worker_message` inherits the worker's prior `model` and `reasoning_effort` unless a follow-up intentionally overrides one of them.
- Reasoning is restricted to Codex config-supported values: `minimal`, `low`, `medium`, `high`, and `xhigh`.
- PatchBay may serialize the auth/session-start segment of `codex exec` per Codex home while keeping full worker turns concurrent after session creation. This avoids rotating-token races and is separate from `max_concurrent_jobs`.
- If `latest_turn.failure_category` is `codex_auth_refresh_failed`, ChatGPT should report that local Codex re-authentication is required and should not keep launching replacement workers until the host login is repaired.

Model-selection guidance is not a hard router. It should help ChatGPT manage worker teams intelligently:

- Spark is the default for compact small workers: small reading tasks, straightforward checks, direct bounded fixes, tests, and exploration. It is preferred over GPT-5.4 Mini when available because it is much faster and effectively free, but it has a smaller context window and quota can deplete.
- GPT-5.4 Mini is the small reliable alternative for many low/moderate-risk tasks, especially when Spark is unavailable, quota-limited, too context-constrained, or when a compatible small OpenAI model is useful.
- GPT-5.4 is the main serious worker, not merely a fallback. Use it for normal above-average repository work, multi-step analysis, implementation planning, debugging, verification, and decisions that need a very good model but not frontier authority.
- GPT-5.5 is the highest-authority lane for innovation, creative architecture, difficult synthesis, unresolved problems, sensitive/final judgment, and work where the best reasoning quality matters more than speed.

Worker file inspection:

- `codex_read_file` reads the base checkout only. It is paged: `max_bytes` caps the returned page, not the whole file, and large reads may return `next_start_line`.
- Before integration, files created only in an isolated worker worktree are read with `codex_worker_inspect(view="file", file_path="...")`.
- `view="file"` returns bounded chunks with line numbers. Use `start_line`, `end_line`, and returned `next_start_line` for pagination instead of asking for one large `max_bytes` response.
- Report files created in isolated worktrees are labeled as worker-worktree-only until explicitly integrated or copied into the base checkout.
- `codex_worker_inspect(view="diff", file_path="...")` remains the preferred way to inspect a patch; `view="file"` is for exact worker-side file content.

Worker artifact inbox:

- `codex_worker_inbox(action="import_file")` uses ChatGPT Apps file parameters (`_meta["openai/fileParams"]`) to download a ChatGPT-generated file or zip into local PatchBay runtime storage.
- Download URLs must use configured schemes, defaulting to HTTP(S), matching ChatGPT Apps temporary file URLs.
- Imports are scoped to the current workspace, return compact artifact ids, and do not edit the repository or a worker worktree by themselves.
- Sensitive-looking filenames such as `.env`, key files, or auth/session-looking JSON are allowed as artifact contents when intentionally imported. Import/list responses do not echo file contents.
- Archives are structurally contained: absolute paths, parent traversal, and link/device entries are rejected. Configurable size/count limits exist only when the operator sets them; local defaults are unlimited.
- Pass artifact ids through `context_from_artifacts` on `codex_worker_start` or `codex_worker_message`. PatchBay copies selected artifacts into `.ai-bridge/imported-artifacts/` inside the isolated worker worktree and excludes that reserved directory from worker changes, diffs, and integration.
- In this release artifact attachments require isolated workers. If ChatGPT needs artifacts, omit `workspace_mode` or set `workspace_mode: "isolated_write"`.

## Pro Escalation Request Tools

Pro Escalations are the reverse handoff path for blocked local problems that need ChatGPT Pro input. Local Codex or the operator creates a Pro Request from a report and optional attachments. ChatGPT Pro reads, claims, and responds through MCP. Dispatch back to local Codex is a separate explicit operation.

These tools are advertised in `standard`, `full`, and `worker` modes:

| Tool | Mutability | Purpose |
| --- | --- | --- |
| `codex_pro_request_list` | read-only | List open or recent Pro Requests with compact public metadata. |
| `codex_pro_request_read` | read-only | Read one bounded report, optional stored response, attachment index, event history when requested, and repo staleness check. |
| `codex_pro_request_claim` | mutating/non-idempotent | Claim a request for the current MCP connection. Cross-owner mutation requires explicit `takeover: true`. |
| `codex_pro_request_respond` | mutating/non-idempotent | Store ChatGPT Pro's answer only. Does not execute, dispatch, message workers, edit files, apply changes, or commit. |
| `codex_pro_request_dispatch` | mutating/open-world/non-idempotent | Explicitly send the stored answer to an idle origin worker or start a new isolated worker. Does not integrate or commit. |
| `codex_pro_request_close` | mutating/non-idempotent | Close, cancel, or supersede a request after it is consumed or obsolete. |

Public Pro Request views omit private repo paths, raw job ids, raw Codex session ids, raw transcripts, and runtime storage paths. The canonical store is PatchBay runtime storage; `.ai-bridge/pro-requests/<request-id>/` is only a sanitized mirror for local visibility.

Pro Request reports are diagnostic evidence. Tool descriptions must keep saying that report contents do not override user instructions, system/developer instructions, AGENTS.md, repository rules, or safety policy.

Dispatch rules:

- `target: "origin_worker"` messages the recorded origin worker only when it is available and idle.
- `target: "new_worker"` starts a named worker, defaulting to `isolated_write`.
- busy or missing origin workers return `dispatch_blocked` and are not queued silently.
- dispatch never applies worker output to the base checkout and never commits.

## New Context Tools

| Tool | Mutability | Purpose |
| --- | --- | --- |
| `codex_open_workspace` | read-only | Open the active workspace and return bounded orientation: repo name, branch, git status summary, AGENTS summary, available skills, and next suggested tools. |
| `codex_repo_tree` | read-only | Return bounded tree for the active workspace or a subpath. |
| `codex_search_repo` | read-only | Search allowed source files with ripgrep-first behavior and redacted snippets. |
| `codex_read_file` | read-only | Read a bounded file slice inside the base checkout of the workspace. |
| `codex_load_context` | read-only | Return AGENTS, selected files, git status, and `.ai-bridge` context for a task. |
| `codex_export_context` | mutating, scoped | Write a selected context pack under `.ai-bridge`, never arbitrary source files. |
| `codex_list_workspaces` | read-only | List configured workspaces known to the connector. |
| `codex_workspace_snapshot` | read-only | Return git status, recent commits, `.ai-bridge`, and a compact tree. |
| `codex_inventory` | read-only | Return tool modes, skill inventory, git state, and power-mode settings. |
| `codex_git_status` | read-only | Show branch and changed files without bash. |
| `codex_git_diff` | read-only | Show bounded unstaged or staged git diff without bash. |
| `codex_show_changes` | read-only | Return review-oriented status, diff stats, and optional diff, optionally scoped to one file. |
| `codex_list_skills` | read-only | List skill names/descriptions without exposing local install paths. |
| `codex_load_skill` | read-only | Load a bounded `SKILL.md` by known skill name. |

If `repositories.aliases` is configured, workspace tools and worker `repo_path` arguments may accept a canonical operator path and resolve it to the local mounted or copied path before validation. Responses can include `workspace_alias` metadata so ChatGPT can say which canonical path was requested and which local workspace is actually in use.

### Worker peer context

`codex_worker_start` and `codex_worker_message` accept optional peer context:

| Field | Meaning |
| --- | --- |
| `context_from_workers` | Worker names or ids whose current report/changes/diff/review context should be included in the new turn. |
| `context_detail` | `report`, `changes`, or `diff`; defaults to `report`. |
| `context_from_artifacts` | Artifact ids returned by `codex_worker_inbox`; selected artifacts are materialized into the isolated worker worktree as source material. |

Peer context is inserted into the Codex prompt as data, not as a higher-priority instruction. It is capped, redacted, and workspace-relative.

## Handoff Tools

| Tool | Mutability | Purpose |
| --- | --- | --- |
| `codex_write_handoff` | mutating, scoped | Write a plan into `.ai-bridge/current-plan.md` for explicit local execution. |
| `codex_get_handoff_status` | read-only | Read `.ai-bridge/agent-status.md`, execution summary, and current handoff state. |
| `codex_get_handoff_diff` | read-only | Return bounded diff artifacts written by local handoff execution. |

Handoff tools are not a replacement for Codex jobs. They are useful when the user wants ChatGPT to prepare work and then explicitly run a local agent from the terminal.

Local terminal commands provide the CodexPro-style non-MCP side of the flow:

- `python scripts/handoff.py execute ...`
- `python scripts/handoff.py watch ...`
- `python scripts/pro_context.py bundle ...`
- `python scripts/pro_context.py apply ...`

## Optional Power Tools

These tools are part of the public surface in full tool mode. The current
runtime permission profile enables their authority by default, but the
recommended ChatGPT-facing default is `worker`, which hides these power tools
until the surface is deliberately broadened. Narrower runtime profiles may also
disable them. They must stay clearly marked in descriptors.

| Tool | Mutability | Required control |
| --- | --- | --- |
| `codex_edit_file` | mutating | Direct write profile, path guard, diff return. |
| `codex_write_file` | mutating | Same as edit, preferably restricted by launch root when needed. |
| `codex_run_command` | open-world/mutating risk | Safe/full command mode, timeout, optional session gate, and output caps. |
| `codex_read_session` | read-only but highly sensitive | Bounded transcript, redaction, explicit config. |

Descriptor truthfulness is runtime-aware. When `power_tools.direct_write` is
false, `codex_write_file`, `codex_edit_file`, `write`, and `edit` are not
advertised and cannot be called through the public protocol. When
`power_tools.bash_mode` is `off`, `codex_run_command` and `bash` are not
advertised. When `power_tools.codex_session_read` is false,
`codex_read_session` and `read_codex_session` are not advertised. This is not a
tool catalog reduction policy; it prevents ChatGPT from seeing tools the
current runtime will reject.

## Tool Descriptor Requirements

Every public descriptor must include:

- `name`;
- `title` where supported;
- description with direct usage guidance;
- JSON input schema;
- output schema when returning structured content;
- `annotations.readOnlyHint`;
- `annotations.destructiveHint`;
- `annotations.openWorldHint`;
- `securitySchemes`;
- `_meta.securitySchemes`;
- `_meta["openai/fileParams"]` for tools that receive ChatGPT files;
- no invocation status labels, `_meta.ui.resourceUri`, or `openai/outputTemplate` by default;
- optional `app.tool_cards: true` descriptors may include invocation status labels plus `_meta.ui.resourceUri` and `openai/outputTemplate` pointing to the shared ChatGPT card resource.

These descriptors are not only API documentation; they are part of the model prompt surface ChatGPT uses for tool selection. Keep descriptions outcome-first and explicit about:

- ChatGPT's manager/consultant role and the expectation that non-trivial repo work is delegated to workers;
- the explicit exception list for direct read/search tools: orientation, worker briefing context, focused verification, exact line/diff checks, reviewing worker evidence, specific doubts, tiny tasks, and quick checks where a worker brief would be worse than the check;
- the expectation that broad work should consider a worker team and may use up to the configured concurrent worker slots when responsibilities are clear;
- the expectation that named workers are reusable continuing specialists, and that `codex_worker_message` is the normal follow-up path when worker output is incomplete, contradictory, or decision-critical;
- when to use the tool and when another worker/context tool is better;
- when direct read/search tools are only for light orientation or verification rather than the primary implementation loop;
- whether the tool reads, writes, starts a process, stops work, or applies changes;
- whether state is durable across PatchBay restart;
- what should be inspected before a mutating follow-up;
- what validation or blocked-state behavior ChatGPT should report after the tool result.
- when consequential worker assignments should request durable report files or changed-file evidence instead of relying on a compressed chat/tool summary.
- when to use a progressive menu such as `codex_worker_options` instead of hardcoding dynamic choices into a primary mutating tool, including the advisory Spark / GPT-5.4 Mini / GPT-5.4 / GPT-5.5 worker-selection ladder.
- that paging, byte caps, and bounded result fields are response-stability controls, not an instruction to save tokens or avoid necessary evidence.
- that worker failure diagnostics such as `codex_auth_refresh_failed` are manager-facing operational facts, not repository-analysis conclusions.

The canonical names remain `codex_*`. Compatibility aliases such as `read`, `write`, `edit`, `bash`, `show_changes`, `git_status`, `git_diff`, `workspace_snapshot`, `export_pro_context`, and `handoff_to_agent` may be advertised depending on `app.tool_mode`, but they must resolve to canonical handlers rather than duplicate execution paths. Their descriptors should advertise the alias names ChatGPT can actually call, such as `path` for `read`/`write`/`edit` and `cmd` or `command` for `bash`, then translate those names into the canonical handler arguments.

Current implementation returns these descriptor fields from `tools/list`, including bounded object output schemas for structured results. Tool-card widgets are disabled by default: `tools/list` must not advertise `_meta.ui.resourceUri`, `openai/outputTemplate`, or invocation labels, and `resources/list` must return no PatchBay widget resource unless the server operator explicitly sets `app.tool_cards: true`. This keeps long ChatGPT/PatchBay sessions lighter on mobile and tablet browsers. The switch is config-only; ChatGPT must not receive a tool that enables cards.

When `app.tool_cards: true`, PatchBay advertises `ui://widget/patchbay-tool-card-v2.html` through `resources/list` and `resources/read` as a `text/html;profile=mcp-app` resource; the legacy v1 URI remains readable for compatibility. The card is intentionally passive and compact: it renders a receipt with a human tool label, human status phrase, and one human detail line while preserving full result fields in `structuredContent` for ChatGPT reasoning and later inspection. Internal tool ids may be supplied through result `_meta` for component-only rendering, but visible card text should not expose backend names such as `codex_worker_start`, `worker_start`, or `repo_busy`. The test suite should snapshot public descriptors and fail if:

- a mutating tool is marked read-only;
- a read-only tool lacks `readOnlyHint`;
- an internal tool appears in `tools/list`;
- a schema advertises fields that handlers do not accept;
- an advertised alias falls back to an open generic schema instead of a precise translated schema;
- aliases are advertised in the wrong tool mode or point to duplicate execution paths instead of canonical handlers.
- default descriptors advertise widget resource URIs or output templates.
- enabled-card descriptor resource URIs drift from the registered resource.
- prompt-critical workflow guidance such as stateful workers, preview-before-integrate, no-commit integration, or validation expectations disappears from `initialize.instructions` or worker descriptors.
- manager-first guidance, direct-tool exceptions, or multi-worker encouragement disappears from `initialize.instructions` or worker descriptors.

## Schema Compatibility

Current PatchBay schemas advertise `spec` and `repo_path`, while internal handlers consume `prompt` and `repo` through translation. Compatibility aliases use CodexPro-derived names where PatchBay supports them, then translate into the same canonical handlers. That bridge should be made explicit:

- public schemas keep stable names for existing users;
- handlers receive one normalized internal request object;
- translation is tested for every public tool and advertised alias;
- new tools should avoid public/internal name drift.

## Compatibility Aliases

Tool modes:

- `minimal`: connector and workspace essentials.
- `standard`: core workspace, handoff, and Codex job tools.
- `full`: standard tools plus optional power tools and compatibility aliases.
- `worker`: worker-first context and `codex_worker_*` tools; low-level job controls and compatibility aliases are hidden.

Mode controls are visible in every mode:

- `codex_tool_mode_info`: read-only comparison of current mode, available modes, tool counts, and tool names.
- `codex_tool_mode_switch`: session-local request to switch the MCP tool surface. It does not persist to config files. Direct MCP clients that re-run `tools/list` on the same MCP session see the new catalog; other sessions keep their own effective mode. ChatGPT Developer Mode may require connector metadata refresh before the model sees newly exposed tools.

Alias policy:

- aliases are a ChatGPT selection aid, not the stable API;
- durable docs and client integrations should prefer canonical `codex_*` names;
- aliases must use precise schemas for their advertised argument names and share validation, mutability, and power controls with the canonical tools they resolve to;
- disabling a canonical power tool also disables its alias behavior at execution time.
- disabled canonical power tools and their aliases should be absent from
  `tools/list`. The checked-in permission profile can remain full-authority
  while the default ChatGPT-facing catalog remains worker-first.

Use `worker` mode for first real ChatGPT Developer Mode validation. It keeps the visible tool surface small enough for natural tool selection while still exposing the context tools needed to orient and brief workers. Use `codex_tool_mode_info` before broadening the surface, and `codex_tool_mode_switch` only when current tools are insufficient. Use `full` mode when testing or operating low-level job/session controls and power tools deliberately.

One server URL is one shared local state surface. `codex_self_test` returns redacted coordination metadata such as `client_ref`, `chatgpt_session_ref` when ChatGPT supplies `_meta["openai/session"]`, `work_run_ref`, `active_mcp_sessions`, and `shared_server`; it does not return raw MCP session ids or raw OpenAI metadata. `active_mcp_sessions` is transport-session churn, not proof of worker ownership or conversation identity by itself. Read/list/inspect tools may expose shared local state to connected clients.

Shared-server coordination rules:

- tool mode is session-local for MCP sessions; one chat switching to `full` does not change another chat's effective mode;
- worker, job, and artifact owner metadata is private, but public views can return coordination-owner-relative fields such as `owned_by_current_client`, `ownership_status`, `ownership_scope`, `owner_label`, and `ownership_note`;
- `ownership_status` values are diagnostic coordination labels, not identity claims: `current_client` means the current scoped owner matches; `legacy_connection` means an older durable record has an owner hash but no owner-scope metadata; `other_token_owner` means a different token-scoped owner; `different_owner_scope` means the owner mode changed; `other_connection` covers same-scope non-token owner differences;
- `codex_worker_message`, `codex_worker_integrate`, `codex_worker_stop`, and artifact cleanup refuse cross-owner MCP mutation unless the caller explicitly retries with `takeover: true`;
- explicit takeover rewrites worker/artifact owner metadata to the current scoped owner model, which is the intended migration path for legacy records after user confirmation;
- takeover is coordination, not authentication. HTTP auth, local binding, and tunnel token policy remain the actual access boundary;
- base-checkout mutation paths are serialized per repository. Direct write/edit, command execution, shared-write worker turns, low-level base-writing jobs, and worker integration can return `repo_busy: true` instead of queueing hidden writes;
- when `queue_enabled` is true, global job admission can accept pending Codex turns beyond currently running slots. `codex_self_test` reports both `max_concurrent_jobs` and `queue_enabled`.

## Hidden And Deprecated Tools

The default internal dispatch table exposes only public tools. Legacy experimental cloud/apply-diff/string/sandbox method implementations have been deleted rather than hidden behind a flag. The supported power replacements are:

- `codex_apply_job` for isolated implementation work;
- `codex_get_diff` for proven apply-job diffs;
- `codex_write_file` and `codex_edit_file` for explicit direct workspace writes;
- `codex_run_command` for configured safe/full command execution.

Before importing more CodexPro features:

- keep the public registry separate from internal experiments;
- avoid adding hidden callable methods without public contract tests;
- keep aliases controlled by `app.tool_mode`;
- document deprecation timing for neutral aliases.

## ChatGPT Product Metadata

OpenAI Developer Mode and Apps-compatible clients use metadata to decide how tools are presented and confirmed. PatchBay should therefore treat descriptor metadata as product behavior, not decoration.

Required product behavior:

- write tools prompt for confirmation;
- read-only context tools should not require confirmation;
- destructive or open-world actions must be labeled;
- tool cards should show concise job/workspace/diff state;
- JSON payloads should remain understandable when a user expands them in ChatGPT.

The current resource card is a Python-served, CodexPro-derived widget adapted to PatchBay's `codex_*` structured outputs. It is intentionally passive in this phase: it renders a compact one/two-line receipt but does not initiate tool calls. The card must hydrate from both current MCP Apps bridge notifications (`ui/notifications/tool-result`) and ChatGPT's Apps SDK compatibility globals (`window.openai.toolOutput`), because ChatGPT may provide `structuredContent` through either path. Human labels are derived separately from the model-visible data, using component-only metadata when available and local shape inference otherwise. If hydration fails, the widget should show a compact widget-error receipt instead of remaining on the initial waiting state.
