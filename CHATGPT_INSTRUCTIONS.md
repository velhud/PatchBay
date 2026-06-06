# MCP Client Instructions

This server exposes Codex CLI workflows through MCP Streamable HTTP.

## Endpoint

```text
http://127.0.0.1:8000/mcp
```

## Important Rules

- Only use repositories listed under `repositories.allowed` in `config.yaml`.
- Prefer read-only analysis unless the user explicitly asks for file edits.
- For edits, inspect the returned worktree path and diff before merging changes.
- Do not request `dangerously_bypass`; it is disabled by default.

## Common Workflow

1. Call `get_system_config`.
2. Call `query_text_analytics` for analysis.
3. Call `fetch_operation_result` with the returned `reference_id`.
4. For edits, call `update_content_record`.
5. Review changes with `fetch_record_delta`.

## Tool Mapping

The public tool names are neutral aliases:

- `query_text_analytics` starts a Codex plan job.
- `update_content_record` starts a Codex apply job in an isolated git worktree.
- `fetch_operation_result` returns completed job output.
- `fetch_record_delta` returns a diff for a changed file.
- `analyze_content_changes` runs Codex review.
