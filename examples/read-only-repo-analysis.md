# Read-only Repository Analysis

Goal: let ChatGPT or another MCP client inspect an owned repository without modifying files.

Workflow:

1. Start the local server against an allowed repository.
2. Confirm the repository path is under `repositories.allowed`.
3. Call `codex_open_workspace` and relevant context tools.
4. Call `codex_plan_job`.
5. Poll with `codex_get_status`.
6. Fetch output with `codex_get_result`.
7. Do not run apply or power tools.

Example request:

```json
{
  "spec": "Summarize the repository architecture and identify missing tests.",
  "repo_path": "/absolute/path/to/owned/repo"
}
```
