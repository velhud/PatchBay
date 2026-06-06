# Security Model

`codex-mcp-wrapper` is local-first maintainer infrastructure. It connects MCP-compatible clients to Codex CLI workflows and may interact with local repositories, git worktrees, subprocesses, prompts, diffs, logs, and local configuration.

## Scope

This project is intended for repositories the operator owns, maintains, administers, or is authorized to review.

## Trust Boundaries

- MCP client
- local HTTP server
- configured repository roots
- Codex CLI process
- git worktree used for apply jobs
- local config and environment
- audit logs and job logs
- generated diffs before merge

## Security Goals

- Bind locally by default.
- Treat repository paths as explicit trusted roots.
- Avoid exposing arbitrary filesystem access.
- Avoid logging secrets.
- Keep read-only analysis as the default safe mode.
- Run apply jobs in isolated git worktrees.
- Make generated diffs visible before integration.
- Require explicit user control for risky operations.
- Keep dangerous bypass disabled by default.
- Keep public examples conservative.

## Main Risks

- Path traversal outside the configured repository root.
- Accidental exposure of secrets from local files or environment variables.
- Unsafe subprocess invocation.
- Confused-deputy behavior from an MCP client.
- Applying changes directly to a working branch without review.
- Running the server on a network interface without authentication.
- Malicious local webpage calling a permissive local endpoint.
- Prompt, config, or logging leakage.
- Overbroad repository roots.
- Mutating tools called without explicit user intent.

## Mitigations

- Bind to `127.0.0.1` by default.
- Require configured repository roots.
- Require git repositories by default.
- Default to read-only sandbox.
- Disable dangerous bypass by default.
- Use isolated git worktrees for apply jobs.
- Disable CORS by default.
- Redact audit logs.
- Do not return raw local Codex config.
- Validate extra paths against allowed roots.
- Mark mutating tools as non-read-only.
- Return MCP instructions that describe safe workflow constraints.

## Non-goals

- This project is not a remote multi-user SaaS service.
- This project is not intended for unauthorized scanning.
- This project is not intended to store secrets in prompts or logs.
- This project does not claim to be safe as a public hosted service without authentication, network isolation, and additional review.

## Planned Hardening

- Add command allowlisting around Codex CLI invocation.
- Add optional local bearer-token authentication.
- Add stricter origin-header validation.
- Add CI checks for dependency vulnerabilities.
- Add CodeQL or equivalent static analysis.
- Add security test fixtures.
