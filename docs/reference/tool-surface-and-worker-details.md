# Tool Surface And Worker Details

This page holds the detailed public tool, worker, status, ownership, power-tool, and tool-card notes that were removed from the root README.

The root README should describe the product. This page is for operators and MCP/client implementers who need the deeper surface contract.

## Public MCP tool tiers

The canonical public names are `codex_*`.

In `full` tool mode, compatibility aliases such as `read`, `write`, `edit`, `bash`, `show_changes`, `git_status`, `git_diff`, `workspace_snapshot`, `export_pro_context`, and `handoff_to_agent` can also be advertised. Aliases resolve to the canonical handlers and expose CodexPro-derived input schemas adapted to PatchBay argument names.

Use `--tool-mode worker` for a worker-first surface that hides low-level job/session controls and compatibility aliases while keeping worker tools plus the context tools needed to brief them. In this mode, ChatGPT should act as a manager and engineering lead. For non-trivial repository, document, codebase, architecture, audit, debugging, implementation, or review work, appoint named Codex workers through natural-language briefs and synthesize their reports.

Direct read/search tools remain available for orientation, briefing context, focused verification, exact line/diff checks, reviewing worker evidence, specific doubts, or tiny tasks where a worker would be unnecessary. They should not become the main execution loop for broad work.

## Natural-language workers

| Tool | Purpose | Read-only |
| --- | --- | --- |
| `codex_worker_options` | Return a bounded Codex model/reasoning menu for worker setup without exposing raw config/catalog data | yes |
| `codex_worker_inbox` | Import ChatGPT-generated files or zips into local artifact context, list/inspect them, or clean up local copies | no |
| `codex_worker_start` | Start a named Codex colleague with an English brief; defaults to an isolated writing worktree | no |
| `codex_worker_message` | Continue or redirect the same Codex conversation by worker name in the same workspace | no |
| `codex_worker_list` | List current-scope workers with compact `team_status`, liveness lines, checkpoints, latest report, and hidden-history count | yes |
| `codex_worker_status` | Show the compact pull-based status bar for the current work run plus live/problem workers | yes |
| `codex_worker_wait` | Wait once, then return a fresh compact worker status without rapid polling | yes |
| `codex_worker_inspect` | Read one worker's report, compact/status/diagnostics state, changed files, worker-created file content, one-file diff, or integration preview | yes |
| `codex_worker_integrate` | Apply an explicitly accepted isolated worker result to the base checkout without committing or deleting the worktree | no |
| `codex_worker_stop` | Stop the active turn, with confirmation for live/recent turns, and optionally discard an isolated worker workspace | no |

Workers are derived from persisted job records and Codex sessions. Human worker names are scoped to the base workspace, so `Small Implementer` can exist in more than one repo. Pass `repo_path` or use the public `worker_id` only when a name is ambiguous.

ChatGPT should treat workers as local assistants, not low-level commands: ask natural questions, assign goals and deliverables, and let workers find the relevant repository details unless exact paths matter.

Workers are continuing specialists, not disposable one-shot summaries. If a report is thin, contradictory, missing evidence, missing validation, or important enough to drive a decision, continue the same worker with `codex_worker_message` before final synthesis.

Worker result reports should include a concise `summary`, substantive `detailed_report`, concrete `evidence`, changed files, commands/tests, notes, risks, open questions, and next steps. PatchBay surfaces those fields in the public report instead of reducing them to a one-line summary.

For larger work, ChatGPT should consider a team rather than one shallow worker: source/folder investigators, implementation owners, reviewers, verification workers, and a synthesis worker using `context_from_workers`.

## Worker model and reasoning options

When ChatGPT needs control over the underlying Codex model or reasoning depth, it should call `codex_worker_options` and then pass `model` and/or `reasoning_effort` to `codex_worker_start`.

The model ladder is advisory:

- Spark is the default for compact small workers because it is fast and effectively free.
- GPT-5.4 Mini is the small reliable alternative.
- GPT-5.4 is the main serious worker for normal above-average tasks.
- GPT-5.5 is the highest-authority lane for innovation, creative architecture, unresolved problems, sensitive/final judgment, and unusually hard synthesis.

Follow-up `codex_worker_message` calls inherit the worker's prior model/reasoning choices unless explicitly overridden and can attach later imported artifacts.

## Worker artifacts and isolated worktrees

When ChatGPT has generated a plan, file, or zip that local Codex should use, call `codex_worker_inbox(action="import_file")` and pass the returned artifact id through `context_from_artifacts`. Imports are local context only and can be repeated; they do not edit the repository.

Default writing workers use durable external worktrees with on-demand changed-file, paged file-content, and one-file diff inspection. Before integration, `codex_read_file` reads only the base checkout. Use `codex_worker_inspect(view="file", file_path="...")` to read a worker-created file from its isolated worktree.

Imported artifacts are copied into `.ai-bridge/imported-artifacts/` inside the isolated worker worktree and excluded from changes, diffs, integration previews, and applies.

Worker start/message calls can include bounded `report`, `changes`, `diff`, or `review` context from other workers. Use `review` when another worker needs report plus changed-file inventory plus bounded diff for review before integration.

`codex_worker_start(auto_suffix=true)` can rerun a phase with a reused human name. `include_untracked_from_base` can copy selected accepted untracked base files into a new isolated worker. Unchanged copied baseline files are treated as context and excluded from integration patches. If the worker edits one of those copied baseline files, integration preview reports `modified_included_untracked_base_files` and blocks automatic apply so ChatGPT can ask for a separate patch, integrate manually, or commit/track the base context first.

## Worker status and polling

`codex_worker_list` returns concise `team_status` plus `team_report`. `codex_worker_status` returns only the compact status bar: counts, deltas since the last check for the same work run/conversation owner, suggested action, one short line per worker, and polling guidance.

Default `scope=current` is deliberately not the full archive: it shows the current work run plus live/problem workers and reports how many old completed/stopped workers are hidden. Use `scope=conversation` to intentionally reuse earlier workers from the same ChatGPT conversation, `scope=recent` for recently active workers, and `scope=history` only when the durable archive is needed.

If `repo_path` is omitted, worker list/status/wait cover all allowed repositories so active work is not hidden by the default workspace. Pass `repo_path` when deliberately narrowing to one repo.

For ordinary monitoring, ChatGPT should wait about 20-30 seconds between status calls and follow `recommended_next_poll_seconds`. Polling every few seconds is reserved for explicit near-real-time requests or immediate recovery from a lost/failed status. If ChatGPT calls status too soon, PatchBay returns a cached `poll_too_early: true` response with `status_current: false` and `retry_after_seconds` without resetting activity deltas.

`codex_worker_wait` is the preferred patient path: it waits once, raises too-small `wait_seconds` values to the configured minimum cadence, then returns a fresh compact status without interrupting workers or exposing raw logs.

`codex_worker_stop` is an interruption, not a status tool. When the latest turn still looks live or is inside `workers.stop_confirmation_grace_seconds`, PatchBay returns `stop_confirmation_required: true` and leaves the worker running. ChatGPT should wait or use `codex_worker_wait` unless it has deliberately decided to interrupt; only then should it repeat the stop with `force: true`.

Before final synthesis on substantial work, ChatGPT should check the relevant worker status and either stop/supersede stale unneeded workers or explicitly report that a worker remains active and why.

## Streaming, liveness, and recovered evidence

PatchBay streams Codex JSON events while a worker turn is running. When Codex emits `thread.started`, the worker's session is recorded immediately rather than only after completion.

`codex_worker_inspect(view="report")` is the normal worker-answer view and omits low-level `latest_turn` internals. `view="status"` is the single-worker liveness/turn-diagnostics view. `view="diagnostics"` exposes the full bounded lifecycle payload for explicit debugging. Full diagnostics include process pid, launch/process timestamps, last event, phase, event count, stdout/stderr bytes seen, command preview, progress, heartbeat age, exit code, session-created status, and classified failure categories when Codex fails before useful work.

Useful `agent_message` events become bounded manager-level checkpoints under `latest_checkpoints` and the latest short partial note under `latest_partial_note`. `liveness.status` uses the compact manager categories `starting`, `active`, `quiet`, `stale`, `lost`, `completed`, `failed`, and `cancelled`.

A missing final report is not automatically a stuck worker. PatchBay persists a result artifact even when Codex does not emit the final structured result event; it falls back to the latest agent message, a bounded raw-output note/preview, or a redacted failure diagnostic so cancelled, failed, and unusual turns still have manager-readable evidence. `report_artifacts` expose `result_source`, `codex_result_event_seen`, `turn_completed_seen`, and `parsed_output_schema_valid` so the manager can tell a final schema result from a usable assistant-message fallback or raw-output fallback.

## Ownership and shared servers

If multiple ChatGPT conversations share one Server URL, worker and artifact views include owner-relative coordination flags.

By default `ownership.scope: token` treats calls using the same bearer/query token as the same coordination owner, so short-lived transport sessions from the same copied connector URL normally continue the same workers without takeover.

When ChatGPT supplies `_meta["openai/session"]`, PatchBay hashes it into `chatgpt_session_ref` and stamps workers with a separate `work_run_ref`. Raw OpenAI metadata is not logged or returned. `active_mcp_sessions` is transport-session churn, not proof of worker ownership or conversation identity by itself.

Public ownership statuses distinguish `current_client`, `legacy_connection`, `other_token_owner`, `different_owner_scope`, and `other_connection`. Read/list/inspect remain shared, but mutating another owner's worker or artifact requires an explicit `takeover: true` call after user confirmation.

When `queue_enabled: true`, Codex turns above `max_concurrent_jobs` remain pending until an execution slot opens. `codex_startup_serialization_enabled: true` adds a narrower gate: only Codex auth/session startup is serialized per effective Codex home, using both an in-process gate and a host file lock, then full worker turns continue concurrently after session creation.

Base-checkout mutation paths, including direct writes, command execution, shared-write workers, and worker integration, still use per-repository mutation locks and return `repo_busy` instead of queueing hidden writes.

PatchBay does not add a worker database, message bus, transcript copy, role engine, automatic reviewer chain, automatic commits, or automatic merge queue.

## Pro Escalation requests

| Tool | Purpose | Read-only |
| --- | --- | --- |
| `codex_pro_request_list` | List open or recent local-to-ChatGPT Pro requests | yes |
| `codex_pro_request_read` | Read one bounded report, response, attachment index, and repo staleness check | yes |
| `codex_pro_request_claim` | Claim the request for the current MCP connection | no |
| `codex_pro_request_respond` | Store ChatGPT Pro's answer only; no execution, dispatch, edit, apply, or commit | no |
| `codex_pro_request_dispatch` | Explicitly send the stored answer to an idle origin worker or start a new isolated worker | no |
| `codex_pro_request_close` | Close, cancel, or supersede a request | no |

Local creation and operator inspection use `patchbay pro-request create/list/show/response/dispatch/close`. The canonical store lives in PatchBay runtime storage; `.ai-bridge/pro-requests/<request-id>/` is a sanitized mirror for local visibility. Dispatch is deliberate and never integrates worker output into the base checkout.

See [docs/pro-escalations/USER_FLOW.md](../pro-escalations/USER_FLOW.md) and [docs/pro-escalations/ARCHITECTURE.md](../pro-escalations/ARCHITECTURE.md).

## Core Codex jobs

| Tool | Purpose | Read-only |
| --- | --- | --- |
| `codex_plan_job` | Start a Codex analysis job using the configured sandbox | no in the full-power profile |
| `codex_apply_job` | Start an isolated Codex apply job in a git worktree | no |
| `codex_get_status` | Inspect async job state | yes |
| `codex_get_result` | Fetch completed job output | yes |
| `codex_get_diff` | Inspect a changed file diff from a completed apply job | yes |
| `codex_cancel_job` | Cancel a pending or running local Codex job | no |
| `codex_review` | Run Codex review on owned changes | yes |
| `codex_interactive` | Start an async Codex exec session job | no |
| `codex_interactive_reply` | Continue a Codex session through an async job | no |
| `codex_resume` | Resume a prior Codex session through an async job | no |
| `codex_list_sessions` | List bounded PatchBay-known and configured Codex-home session metadata without transcripts or source paths | yes |

## Workspace context

| Tool | Purpose | Read-only |
| --- | --- | --- |
| `codex_self_test` | Check connector readiness and Server URL metadata | yes |
| `codex_open_workspace` | Orient ChatGPT to an allowed workspace | yes |
| `codex_list_workspaces` | List configured workspaces | yes |
| `codex_workspace_snapshot` | Return git status, recent commits, `.ai-bridge`, and compact tree | yes |
| `codex_inventory` | Return tool modes, skills, git state, and power-mode settings | yes |
| `codex_repo_tree` | Return a bounded repository tree | yes |
| `codex_read_file` | Read a bounded text file slice | yes |
| `codex_search_repo` | Search the repo with bounded, redacted results | yes |
| `codex_git_status` | Show branch and changed files without bash | yes |
| `codex_git_diff` | Show bounded git diff without bash | yes |
| `codex_show_changes` | Return review-oriented status and optional diff, optionally scoped to one file | yes |
| `codex_load_context` | Load AGENTS, selected files, git, and `.ai-bridge` context | yes |
| `codex_list_skills` | List discovered skills with sanitized paths | yes |
| `codex_load_skill` | Load a bounded discovered `SKILL.md` | yes |

## Handoff and context artifacts

| Tool | Purpose | Read-only |
| --- | --- | --- |
| `codex_export_context` | Write selected context under `.ai-bridge` | no |
| `codex_write_handoff` | Write `.ai-bridge/current-plan.md` | no |
| `codex_get_handoff_status` | Read `.ai-bridge` status artifacts | yes |
| `codex_get_handoff_diff` | Read bounded handoff diff artifacts | yes |

Local handoff commands are available without attaching ChatGPT:

```bash
python scripts/handoff.py execute --root /path/to/repo --agent custom --command-template "my-agent --task-file {{plan_file}}" --yes
python scripts/handoff.py watch --root /path/to/repo --agent custom --command-template "my-agent --task-file {{plan_file}}" --once --yes
python scripts/pro_context.py bundle --root /path/to/repo --path README.md --include-diff
python scripts/pro_context.py apply --root /path/to/repo --file plan.md --agent codex
```

## Optional power tools

These are public capabilities in `full` tool mode. The current runtime permission profile enables their authority by default, but the recommended ChatGPT-facing default is `worker`, which hides these power tools until the surface is deliberately broadened.

| Tool | Required config |
| --- | --- |
| `codex_write_file` | `power_tools.direct_write: true` |
| `codex_edit_file` | `power_tools.direct_write: true` |
| `codex_run_command` | `power_tools.bash_mode: safe` or `full` |
| `codex_read_session` | `power_tools.codex_session_read: true` |

`tools/list` is runtime-aware for these capabilities: if a profile disables direct write, bash, or session transcript reads, the corresponding canonical tools and compatibility aliases are not advertised and calls to them are rejected.

On a dedicated full-access workbench or VM, ChatGPT should treat missing dependencies, repo-local virtual environments, verification commands, commits, and authorized private-repo pushes as normal engineering work when the user asked for an end-to-end result. It should ask first for public, production, paid-resource, credential-changing, or irreversible external actions.

## ChatGPT metadata and tool card

`tools/list` includes the data metadata every public tool needs: `title`, read/write/open-world annotations, top-level `securitySchemes`, `_meta.securitySchemes`, output schemas, and `openai/fileParams` where a tool receives ChatGPT files.

PatchBay does not advertise a widget by default.

PatchBay still contains a compact passive Apps card resource:

```text
ui://widget/patchbay-tool-card-v2.html
```

The card is disabled by default because repeated ChatGPT Apps iframes made long PatchBay sessions heavy and difficult to use on phones and tablets. This is a server/operator configuration choice, not a ChatGPT tool and not something the model can toggle.

Even without tool cards, PatchBay intentionally keeps visible MCP `content` text compact for worker/status/report tools while preserving the full payload in `structuredContent`. This avoids duplicating large worker reports into the chat interface while keeping the model-readable structured result available.

Operators can opt in by setting:

```yaml
app:
  tool_cards: true
```

When enabled, clients can fetch the card with `resources/list` and `resources/read`. The MIME type is `text/html;profile=mcp-app`. The card is a lightweight receipt: it shows a human tool label, a human status phrase, and one human-readable detail line while leaving the full tool payload in `structuredContent` for ChatGPT reasoning and later inspection. It remains passive and does not initiate tool calls.
