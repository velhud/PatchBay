# Local Safety Checklist

Before running:

- Server binds to `127.0.0.1`.
- Public tunnel mode has `CODEX_MCP_HTTP_TOKEN` set.
- CORS is disabled or restricted to a trusted local UI.
- Repository roots are explicit.
- Dangerous bypass is disabled.
- Prompt body logging is disabled.
- Response body logging is disabled.
- Mutating tools require explicit user intent.
- Apply jobs use worktrees.
- Diffs are reviewed before merge.
- Direct edit, bash, and transcript-read power modes are intentionally enabled or left off.
- Real ChatGPT Developer Mode tests start on disposable repositories.
