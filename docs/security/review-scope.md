# Security Review Scope

This project is intended for authorized local use on repositories the operator owns or has permission to maintain.

## In Scope

- `velhud/patchbay`
- other repositories owned or maintained by the operator when explicitly configured in `repositories.allowed`
- local and token-gated tunnel Streamable HTTP MCP usage
- ChatGPT Developer Mode connector behavior
- path validation
- worktree isolation
- prompt and audit logging
- config redaction
- environment handling
- Codex CLI invocation safety
- tool metadata and read/write boundaries
- direct edit, bash, session-read, and tunnel power modes

## Out Of Scope

- scanning third-party repositories without authorization
- public internet deployment without authentication
- production multi-user hosting
- using Codex Security against systems or repositories the operator does not own or maintain
- storing or processing private customer code without authorization

## Default Security Posture

- bind to `127.0.0.1`
- require token auth for non-loopback and tunnel modes
- require configured repository roots
- require git repositories
- read-only sandbox by default
- dangerous bypass disabled
- review diffs before merging generated changes
