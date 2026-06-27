# Threat Model

## Assets

- local repositories
- source code and diffs
- local Codex configuration
- environment variables
- API keys
- generated worktrees
- audit logs
- job logs
- MCP session IDs

## Actors

- trusted local operator
- ChatGPT or another MCP-compatible client
- local webpage or local process
- repository content that may include prompt injection
- accidental misconfiguration
- malicious or compromised local tool
- leaked public tunnel URL or query token

## Primary Threats

1. A local webpage calls the MCP endpoint through permissive CORS.
2. A user configures an overbroad repository root.
3. A prompt or repository file contains secrets and those secrets are logged.
4. A mutating tool is incorrectly treated as read-only.
5. An internal tool is exposed through fallback dispatch.
6. `add_dirs` expands access outside allowed roots.
7. Codex child process inherits unnecessary environment variables.
8. Raw local Codex config is returned to MCP clients.
9. Dangerous bypass is enabled indirectly or under a misleading name.
10. Generated changes are applied without human diff review.
11. A tokenized public MCP URL leaks through logs, screenshots, browser history, or shared chats.
12. ChatGPT uses a power tool whose descriptor does not clearly reflect mutability or open-world behavior.

## Security Invariants

- Public tool surface must be explicit.
- Unknown tools must be rejected.
- Mutating tools must be non-read-only.
- Dangerous bypass must be disabled by default.
- CORS must be disabled or restricted by default.
- Logs must not include full prompts/responses by default.
- Paths must be validated against allowed roots.
- Apply jobs must use worktrees or explicit diffs.
- Human review happens before merge.
- Public tunnel modes must require auth.
- Power tools must be disabled by default and clearly described.
