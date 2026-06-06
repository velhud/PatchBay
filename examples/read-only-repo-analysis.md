# Read-only Repository Analysis

Goal: inspect an owned repository without modifying files.

Workflow:

1. Start the local server.
2. Confirm the repository path is under `repositories.allowed`.
3. Call `codex_plan_job`.
4. Poll with `codex_get_status`.
5. Fetch output with `codex_get_result`.
6. Do not run apply tools.

Example request:

```json
{
  "spec": "Summarize the repository architecture and identify missing tests.",
  "repo_path": "/absolute/path/to/owned/repo"
}
```
