# MCP Client Instructions

This server exposes local-first Codex CLI maintainer workflows through MCP Streamable HTTP.

## Endpoint

```text
http://127.0.0.1:8000/mcp
```

## Important Rules

- Only use repositories listed under `repositories.allowed` in `config.yaml`.
- Prefer read-only analysis unless the user explicitly asks for file edits.
- For edits, inspect the returned worktree path and diff before merging changes.
- Do not pass secrets, API keys, auth files, or private customer data.
- Do not expose this local endpoint to untrusted web origins.

## Common Workflow

1. Call `codex_get_config`.
2. Call `codex_plan_job` for read-only analysis.
3. Call `codex_get_status` with the returned `job_id`.
4. Call `codex_get_result` when the job completes.
5. For edits, call `codex_apply_job` only after explicit user intent.
6. Review changes with `codex_get_diff`.
7. Run tests and merge manually.

## Public Tools

- `codex_plan_job` starts a read-only Codex plan job.
- `codex_apply_job` starts a Codex apply job in an isolated git worktree.
- `codex_get_status` returns async job state.
- `codex_get_result` returns completed job output.
- `codex_get_diff` returns a diff for a changed file.
- `codex_review` runs Codex review on owned or authorized changes.
- `codex_resume` resumes a prior Codex session.
- `codex_interactive` starts a Codex exec session.
- `codex_interactive_reply` continues a Codex exec session.
- `codex_get_config` returns redacted config metadata.
