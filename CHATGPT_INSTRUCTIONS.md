# ChatGPT MCP Client Instructions

This server lets ChatGPT work with local repositories and local Codex through MCP Streamable HTTP. It supports three primary modes:

- direct workspace mode, where ChatGPT reads/searches/orients inside an allowed repo;
- named worker mode, where ChatGPT starts and continues durable Codex colleagues by human name;
- Codex controller mode, where ChatGPT starts local Codex jobs and inspects status, results, diffs, and session refs.

## Endpoint

Local development endpoint:

```text
http://127.0.0.1:8000/mcp
```

Tunnel endpoints must use token auth. Bearer auth is preferred. Query-token URLs are allowed only for copied ChatGPT Server URL flows and must not be logged or shared.

Recommended first ChatGPT launch:

```bash
python scripts/start.py --root /absolute/path/to/disposable/repo --tool-mode worker
```

For public tunnel validation, keep `--tool-mode worker` in the tunnel launch command. Worker mode exposes the natural-language worker tools and the read-only context tools needed to brief them; it hides low-level job/session controls and compatibility aliases. Use `full` mode only when the user explicitly wants power-user controls.

ChatGPT can call `codex_tool_mode_info` to compare tool modes and `codex_tool_mode_switch` to request a process-local mode change. A switch changes the server's `tools/list` response for clients that re-list tools, but real ChatGPT Developer Mode may keep the old visible tool catalog until the connector is refreshed or reconnected.

## ChatGPT App Settings

Open ChatGPT:

```text
Settings
-> Apps
-> Advanced settings
-> Developer mode: on
-> Enforce CSP in developer mode: on
-> Create app
```

Use:

```text
Name: Codex MCP Wrapper
Description: Local workspace and Codex bridge for ChatGPT coding
Connection: Server URL
Server URL: paste the full URL printed by scripts/start.py --reveal-token
Authentication: No Authentication / None
```

The ChatGPT app should use `No Authentication / None` because the wrapper protects `/mcp` with the query token embedded in the copied Server URL. Do not configure OAuth or paste an OpenAI API key into ChatGPT for this connector.

After changing tool metadata or updating the wrapper, open the app settings in ChatGPT and use the refresh action if ChatGPT still shows stale tools.

## Operating Rules

- Use only repositories configured under `repositories.allowed`.
- Start with a disposable repo until the real ChatGPT Developer Mode flow is verified.
- The current checked-in profile is full-power. Treat direct writes, full bash, `danger-full-access`, session reads, and child-process environment inheritance as available unless the launcher/runtime config narrows them.
- Prefer context tools before starting jobs.
- Prefer `codex_worker_start` for durable delegation when ChatGPT wants to manage an ongoing named Codex colleague. The default `isolated_write` mode is for implementation work in a private worktree; use `workspace_mode: "read_only"` for advisory/review workers.
- When the user asks for a specific model, deeper/faster reasoning, or model-sensitive delegation, call `codex_worker_options` first. Then pass `model` and/or `reasoning_effort` to `codex_worker_start`; otherwise omit them and use Codex defaults.
- Use `codex_worker_inspect`, `codex_worker_list`, and `codex_worker_message` instead of asking the user to track low-level job/session ids.
- Worker names are scoped to the current workspace. The same name may exist in another repo; pass `repo_path` or use the public `worker_id` only when disambiguation is needed.
- Use `codex_tool_mode_info` before broadening the visible tool surface. Use `codex_tool_mode_switch` only when the current mode lacks a needed control, and switch back to `worker` after the power-user operation when the host sees the updated catalog.
- Do not assume a tool mode switch has changed ChatGPT's visible buttons until new tools actually appear or the connector metadata has been refreshed.
- Prefer `codex_plan_job` before larger apply work, but remember it uses the configured sandbox and is not guaranteed read-only in the full-power profile.
- Prefer `codex_worker_start` for longer code changes that should continue across feedback turns. Prefer `codex_apply_job` for one-shot code changes when explicit low-level job/diff handling is useful.
- Use direct workspace write/edit when the user wants immediate local file changes.
- Use `codex_run_command` for focused verification or local operations requested by the user.
- Do not request secrets, API keys, Codex auth files, `.env` values, customer data, or private logs.
- Review diffs before merge or copy-back.

## State And Validation Model

- The wrapper owns worker state; ChatGPT should manage workers by human name, not by backend job IDs, session IDs, branch names, or worktree paths.
- Workers survive wrapper restart when their durable state is present. After reconnecting, call `codex_worker_list` before assuming a worker is gone.
- Worker model/reasoning choices are stateful. `codex_worker_message` continues with the worker's prior settings unless ChatGPT deliberately passes a new `model` or `reasoning_effort`.
- A default `isolated_write` worker changes its own external worktree first. The base checkout is not changed until `codex_worker_integrate` succeeds.
- Before accepting a worker result, inspect `view: "changes"`, targeted `view: "diff"`, and `view: "integration_preview"` when applying the result is being considered.
- `codex_read_file` reads the base checkout. Before integration, worker-created files live in the worker workspace; read them with `codex_worker_inspect` using `view: "file"` and `file_path`.
- `codex_worker_integrate` applies accepted changes to the base checkout, does not commit, and preserves the worker worktree.
- After integration or direct edits, review `codex_show_changes` or `codex_git_diff`, then run focused validation with `codex_run_command` when that tool is available. If validation cannot run, report the exact blocker.
- Do not claim a worker changed, validated, integrated, stopped, or cleaned up anything until the matching tool result says so.

## Normal Workflow

1. Call `codex_self_test`.
2. Call `codex_open_workspace`.
3. Call `codex_workspace_snapshot` or `codex_inventory`.
4. Load relevant AGENTS/context with `codex_load_context`.
5. Use `codex_list_skills` and `codex_load_skill` only when a discovered skill is relevant.
6. Use `codex_read_file`, `codex_search_repo`, `codex_git_status`, `codex_git_diff`, and `codex_show_changes` for orientation.
7. If a worker needs a specific Codex model or reasoning effort, call `codex_worker_options` and choose from the returned menu.
8. For durable delegation, call `codex_worker_start` with a human name, natural-language brief, optional `workspace_mode`, and optional `model`/`reasoning_effort`.
9. Inspect the worker with `codex_worker_inspect` or `codex_worker_list`.
10. Continue the same Codex conversation by name with `codex_worker_message`; include `context_from_workers` when another worker's report or diff should be relayed.
11. If the required control is not visible, call `codex_tool_mode_info`, then `codex_tool_mode_switch` only when broadening is justified. If ChatGPT does not receive the new catalog, ask the operator to refresh or reconnect the connector.
12. Use low-level `codex_plan_job`, `codex_get_status`, `codex_get_result`, and session tools for compatibility, debugging, or explicit power-user control.
13. For worker changes, inspect with `codex_worker_inspect` using `view: "changes"`, `view: "file"` with a workspace-relative `file_path` for worker-created file content, `view: "diff"` with a workspace-relative `file_path`, or `view: "integration_preview"` before accepting work.
14. Use `codex_worker_integrate` only for an explicitly accepted isolated writing worker result; review, test, and commit through the normal repository workflow afterward.
15. For low-level one-shot changes, call `codex_apply_job`, then inspect with `codex_get_result` and `codex_get_diff`.
16. If a result includes `session_ref`, continue with `codex_resume` or `codex_interactive_reply`.
17. If a local terminal handoff is preferred, write `.ai-bridge/current-plan.md` with `codex_write_handoff` and let the operator run the local handoff CLI.

## Worker-First Flow

The natural-language worker facade is implemented through Phase 4. Use it when ChatGPT wants to appoint named local Codex colleagues, read reports, restart the wrapper, continue conversations by name, and pass bounded report/change/diff context between workers.

Default workers use `isolated_write`: the wrapper creates one external worker worktree and reuses it across turns. Use `read_only` for investigation/review work that must not edit files. Use `shared_write` only when the user explicitly wants direct base-checkout writes.

Worker coordination is implemented through `context_from_workers` and `context_detail` on `codex_worker_start` and `codex_worker_message`. Use this for reviewer handoffs, alternate implementations, and sending one worker's concern back to another. Worker integration is implemented as an explicit boundary: inspect through `view: "changes"`, `view: "file"`, `view: "diff"`, and `view: "integration_preview"` as needed, then call `codex_worker_integrate` only when the user or ChatGPT deliberately accepts that worker result. Integration applies to the base checkout without committing and preserves the worker worktree.

Worker model and reasoning selection is implemented as a progressive menu. `codex_worker_options` returns bounded model metadata from the installed Codex runtime/catalog and explains which `model` and `reasoning_effort` values can be passed to worker tools. It does not expose raw Codex config paths, provider credentials, prompts, or auth data. Leave these fields empty unless the user or task makes the choice important.

## Tool Tiers

### Workspace context

- `codex_open_workspace`
- `codex_workspace_snapshot`
- `codex_inventory`
- `codex_repo_tree`
- `codex_read_file`
- `codex_search_repo`
- `codex_git_status`
- `codex_git_diff`
- `codex_show_changes`
- `codex_load_context`
- `codex_list_skills`
- `codex_load_skill`

These are the first tools to use when ChatGPT needs to understand the repo.

### Codex job control

- `codex_worker_options`
- `codex_worker_start`
- `codex_worker_message`
- `codex_worker_list`
- `codex_worker_inspect`
- `codex_worker_integrate`
- `codex_worker_stop`
- `codex_plan_job`
- `codex_apply_job`
- `codex_get_status`
- `codex_get_result`
- `codex_get_diff`
- `codex_cancel_job`
- `codex_review`
- `codex_interactive`
- `codex_interactive_reply`
- `codex_resume`
- `codex_list_sessions`

Use these when ChatGPT should delegate work to local Codex.

### Handoff artifacts

- `codex_export_context`
- `codex_write_handoff`
- `codex_get_handoff_status`
- `codex_get_handoff_diff`

These write or read `.ai-bridge` artifacts. They do not automatically execute local agents.

### Optional power tools

- `codex_write_file`
- `codex_edit_file`
- `codex_run_command`
- `codex_read_session`

These require explicit server-side config. If a power tool returns disabled, do not retry it unless the operator enables the matching power mode.

## CodexPro-Compatible Aliases

When compatibility aliases are exposed by `app.tool_mode`, short names such as `read`, `write`, `edit`, `bash`, `show_changes`, `git_status`, `git_diff`, `workspace_snapshot`, `export_pro_context`, `handoff_to_agent`, and `handoff_to_codex` map to canonical `codex_*` tools.

Prefer canonical `codex_*` names in persistent instructions and reports. Use aliases only when they improve ChatGPT tool selection in a live session.

## Result Handling

- Async starters return a `job_id`; always poll before fetching final output.
- `codex_get_result` may include `session_ref`; store it for continuation.
- `codex_get_diff` is only valid for completed apply jobs and changed files.
- `codex_list_sessions` returns metadata only.
- `codex_read_session` is disabled by default because transcripts may contain private prompts, source, or credentials.
