# ChatGPT MCP Client Instructions

This server lets ChatGPT work with local repositories and local Codex through MCP Streamable HTTP. It supports two primary modes:

- direct workspace mode, where ChatGPT reads/searches/orients inside an allowed repo;
- Codex controller mode, where ChatGPT starts local Codex jobs and inspects status, results, diffs, and session refs.

## Endpoint

Local development endpoint:

```text
http://127.0.0.1:8000/mcp
```

Tunnel endpoints must use token auth. Bearer auth is preferred. Query-token URLs are allowed only for copied ChatGPT Server URL flows and must not be logged or shared.

## Operating Rules

- Use only repositories configured under `repositories.allowed`.
- Start with a disposable repo until the real ChatGPT Developer Mode flow is verified.
- Prefer context tools before starting jobs.
- Prefer `codex_plan_job` before any apply or direct edit action.
- Prefer `codex_apply_job` for code changes because it stages changes in an isolated worktree.
- Use direct workspace write/edit only when the operator explicitly enabled `power_tools.direct_write`.
- Use `codex_run_command` only when bash power mode is enabled and the command is focused verification.
- Do not request secrets, API keys, Codex auth files, `.env` values, customer data, or private logs.
- Review diffs before merge or copy-back.

## Normal Workflow

1. Call `codex_self_test`.
2. Call `codex_open_workspace`.
3. Call `codex_workspace_snapshot` or `codex_inventory`.
4. Load relevant AGENTS/context with `codex_load_context`.
5. Use `codex_list_skills` and `codex_load_skill` only when a discovered skill is relevant.
6. Use `codex_read_file`, `codex_search_repo`, `codex_git_status`, `codex_git_diff`, and `codex_show_changes` for orientation.
7. For analysis, call `codex_plan_job`.
8. Poll with `codex_get_status`.
9. Fetch with `codex_get_result`.
10. For changes, call `codex_apply_job`, then inspect with `codex_get_result` and `codex_get_diff`.
11. If a result includes `session_ref`, continue with `codex_resume` or `codex_interactive_reply`.
12. If a local terminal handoff is preferred, write `.ai-bridge/current-plan.md` with `codex_write_handoff` and let the operator run the local handoff CLI.

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
