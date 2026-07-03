# Public Tool Surface

## Design Principle

PatchBay should expose tools as product capabilities, not implementation conveniences. ChatGPT should see narrow, intentional tools that explain when to use them and what control boundary they cross.

The primary ChatGPT posture is lead/manager/consultant, not line-by-line repository implementer or primary repository file reader. The worker-first surface should make ChatGPT ask "Which worker or worker team should I appoint?" for non-trivial repository, Documents, codebase, architecture, audit, debugging, implementation, or review work. Direct context tools remain available and useful for orientation, briefing context, focused checks, exact line/diff verification, reviewing worker evidence, specific doubts, and tiny tasks, but they should not become the main broad-work execution loop.

Delegation is a positive behavior. Tool descriptions and initialize instructions should make it natural for ChatGPT to create multiple named workers when work can be split cleanly. A 10-slot worker configuration should be treated as an opportunity to run investigators, implementers, reviewers, verification workers, and synthesis workers in parallel, not as a limit ChatGPT should avoid approaching for broad tasks.

Repeated direct `codex_read_file` or `codex_search_repo` calls are a negative signal for non-trivial work. They should push ChatGPT back to the manager posture: start or continue a worker, ask it the evidence question, and use direct reads only to verify focused claims or inspect accepted worker evidence.

The manager posture is stateful. ChatGPT should treat named workers as continuing specialists, not disposable one-shot summaries. For consequential work, prompts should ask workers for durable evidence such as report files, changed files, diffs, validation notes, or open-question lists. When a report is thin, contradictory, missing evidence, or decision-critical, ChatGPT should continue the same worker with `codex_worker_message` and use `context_from_workers` for synthesis or cross-review instead of manually copying summaries.

The same public tool surface is served through Streamable HTTP `/mcp` and the stdio entry point (`patchbay stdio` / `patchbay-stdio`). Stdio is a transport compatibility layer; it must not fork tool policy, hidden-tool filtering, schema validation, or session-local tool mode behavior.

Generic `read`, `write`, `edit`, and `bash` aliases are powerful. PatchBay keeps canonical `codex_*` names as the durable API, while `app.tool_mode` can advertise compatibility aliases for ChatGPT live use. Aliases are tool-selection aids, not separate or safer execution paths; they resolve to canonical handlers and use precise alias-specific schemas instead of open generic argument bags.

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

For larger tasks, ChatGPT may start a small team of workers with separate responsibilities such as investigation, backend implementation, UI implementation, tests, review, or integration risk. PatchBay does not impose fixed roles; ChatGPT chooses the management shape and passes bounded report/change/diff context between workers with `context_from_workers`.

| Tool | Mutability | Role |
| --- | --- | --- |
| `codex_worker_options` | read-only | Return a bounded setup menu for Codex worker model and reasoning choices loaded from the installed Codex runtime/catalog. |
| `codex_worker_inbox` | mutating/open-world/destructive/non-idempotent | Import ChatGPT-supplied files or zip packages into a local artifact inbox, list/inspect them, or remove local inbox copies. Import does not edit the repo. |
| `codex_worker_start` | mutating/open-world/non-idempotent | Create a named worker from a natural-language brief and optionally include bounded context from other workers. Defaults to `isolated_write`. |
| `codex_worker_message` | mutating/open-world/non-idempotent | Continue an existing worker by name or id using the prior Codex session and workspace; optionally include bounded context from other workers. |
| `codex_worker_list` | read-only | List known workers with bounded state, latest report, compact `team_report`, and optional filters for active/current-owner/recent/non-stopped workers. |
| `codex_worker_inspect` | read-only | Return one worker's current state, latest report, changed-file inventory, paged worker-created file content, one-file diff, or integration preview, optionally waiting briefly. |
| `codex_worker_integrate` | destructive/non-idempotent | Apply an explicitly accepted isolated writing worker result to the base checkout. Does not commit or delete the worker worktree. |
| `codex_worker_stop` | destructive/non-idempotent | Cancel only the active worker turn while preserving durable identity and prior session continuity; optionally discard an isolated workspace. |

Worker names are scoped to the base workspace. The same human name can be reused in another repo; worker ids remain globally addressable for explicit disambiguation. Worker results omit low-level job ids, Codex session ids, absolute repo/worktree paths, branch names, raw transcripts, and raw process logs. Public worker views include bounded latest-turn diagnostics such as launch/process/session timestamps, tracked process id, exit code, last event, progress, startup-timeout state, and last heartbeat when available. PatchBay streams Codex JSON events while the process is running, so `thread.started` records session creation before final completion. `codex_worker_list` is a lightweight coordination view and does not scan worker worktrees for exact change state; use `active_only`, `include_stopped=false`, `owned_only`, or `created_after` when old workers clutter the view, and use `codex_worker_inspect` change, diff, file, or integration-preview views when exact worker changes matter. Stale durable `running` jobs are reconciled to `failed` only after the grace window and only when PatchBay has neither a live executor task nor a tracked live Codex subprocess for that job. A live process that never emits a Codex JSON session can fail by `codex_session_start_timeout_seconds` without imposing an overall limit on long-running turns when `job_timeout_seconds` is disabled. Worker identity and workspace ownership come from private durable job metadata; peer-worker context is bounded and explicit. Accepted-result integration is explicit and does not commit. PatchBay can queue pending Codex turns behind `max_concurrent_jobs`, but it does not add a worker database, mailbox, queued worker-message delivery, transcript copy, role engine, automatic reviewer chain, automatic commits, or a merge queue.

Recommended ChatGPT worker-management loop:

1. Start with `codex_self_test` and `codex_open_workspace`.
2. Use read-only context tools only enough to understand the allowed workspace and constraints.
3. Start one or more named workers with outcome, context, constraints, deliverables, and report format.
4. For important work, ask workers to create a durable report file or changed-file evidence in the worker workspace.
5. Inspect worker reports and exact changes.
6. Continue the same worker with `codex_worker_message` when evidence is weak, contradictory, missing validation, or needs another worker's report.
7. Use `context_from_workers` for synthesis, review handoff, and cross-worker reconciliation.
8. Preview integration before applying accepted isolated-worker work.

Worker workspace modes:

- `isolated_write`: default; one external worker worktree reused across turns.
- `read_only`: advisory/review mode with a forced read-only Codex sandbox.
- `shared_write`: explicit direct-workspace mode.

Worker execution options use progressive disclosure:

- `codex_worker_options` is the read-only menu tool. It can load the current Codex model catalog through `codex debug models` or the local Codex model cache, then returns only bounded public metadata.
- `codex_worker_start` accepts optional `model` and `reasoning_effort`; omit them to use Codex defaults.
- `codex_worker_message` inherits the worker's prior `model` and `reasoning_effort` unless a follow-up intentionally overrides one of them.
- Reasoning is restricted to Codex config-supported values: `minimal`, `low`, `medium`, `high`, and `xhigh`.

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
| `context_from_workers` | Worker names or ids whose current report/change/diff context should be included in the new turn. |
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
- invocation status labels;
- `_meta.ui.resourceUri` and `openai/outputTemplate` pointing to the shared ChatGPT card resource.

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
- when to use a progressive menu such as `codex_worker_options` instead of hardcoding dynamic choices into a primary mutating tool.
- that paging, byte caps, and bounded result fields are response-stability controls, not an instruction to save tokens or avoid necessary evidence.

The canonical names remain `codex_*`. Compatibility aliases such as `read`, `write`, `edit`, `bash`, `show_changes`, `git_status`, `git_diff`, `workspace_snapshot`, `export_pro_context`, and `handoff_to_agent` may be advertised depending on `app.tool_mode`, but they must resolve to canonical handlers rather than duplicate execution paths. Their descriptors should advertise the alias names ChatGPT can actually call, such as `path` for `read`/`write`/`edit` and `cmd` or `command` for `bash`, then translate those names into the canonical handler arguments.

Current implementation returns these descriptor fields from `tools/list`, including bounded object output schemas for structured results. It advertises `ui://widget/patchbay-tool-card-v2.html` through `resources/list` and `resources/read` as a `text/html;profile=mcp-app` resource; the legacy v1 URI remains readable for compatibility. The current card is intentionally passive but no longer generic: it renders worker reports, artifact inbox summaries, job status, diffs, command/write results, integration previews, ownership/takeover state, and `repo_busy` lock state. The test suite should snapshot public descriptors and fail if:

- a mutating tool is marked read-only;
- a read-only tool lacks `readOnlyHint`;
- an internal tool appears in `tools/list`;
- a schema advertises fields that handlers do not accept;
- an advertised alias falls back to an open generic schema instead of a precise translated schema;
- aliases are advertised in the wrong tool mode or point to duplicate execution paths instead of canonical handlers.
- descriptor resource URIs drift from the registered resource.
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

One server URL is one shared local state surface. `codex_self_test` returns redacted coordination metadata such as `client_ref`, `active_mcp_sessions`, and `shared_server`; it does not return raw MCP session ids. `active_mcp_sessions` is transport-session churn, not proof of worker ownership by itself. Read/list/inspect tools may expose shared local state to connected clients.

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

The current resource card is a Python-served, CodexPro-derived widget adapted to PatchBay's `codex_*` structured outputs. It is intentionally passive in this phase: it renders bounded result state but does not initiate tool calls.
