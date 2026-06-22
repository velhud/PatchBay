# Security And Product Boundary

## Framing

Security in this project is not a reason to weaken the product. It is the control system that lets the product be powerful enough for real ChatGPT-to-local-Codex work.

Broken boundaries reduce usable power:

- a leaked connector token means the user cannot safely keep the bridge running;
- a path escape means ChatGPT cannot be trusted with repo context;
- bad read-only metadata means ChatGPT asks for too many confirmations or skips needed confirmations;
- raw prompt/session logging means users cannot use the tool on serious work.

The goal is maximum useful capability with explicit control.

## Trust Boundaries

| Boundary | Risk | Required control |
| --- | --- | --- |
| ChatGPT to MCP server | Remote tool calls into local machine | Auth, narrow tools, request caps, clear descriptors |
| MCP server to workspace | Local source/data exposure | Allowed roots, path guard, blocked globs, redaction |
| MCP server to Codex CLI | Agent execution | Sandbox policy, env allowlist, command builder tests |
| Codex CLI to worktree | Local writes | Isolated worktrees, diff review, cleanup policy |
| Handoff watcher | Plan becomes local execution | Explicit local command, dry-run, status artifacts |
| Public tunnel | Internet-exposed MCP endpoint | Token required, no `--no-auth`, rotation, warnings |
| Session history | Private transcript exposure | Default off, metadata first, bounded reads |

## Auth And Tunnel Policy

Localhost-only mode may support no authentication if explicitly configured. Any non-loopback bind address or public tunnel must require authentication.

Minimum policy:

- bearer token support for all MCP requests;
- copied ChatGPT URL may include a token only when the user explicitly chooses that flow;
- tokens are generated with sufficient entropy;
- tokens are never printed without warning;
- query-token URLs are not written to logs;
- saved launcher profiles strip token-like keys and keep runtime files outside the repository;
- public tunnel startup fails closed without auth;
- `--no-auth` is rejected for public tunnel mode;
- launcher-managed tunnel processes are terminated together with the local MCP server;
- tunnel binaries are not auto-installed by this wrapper;
- CORS stays disabled unless a trusted local UI requires it.

Future app-store or multi-user use should implement OAuth 2.1 rather than treating a URL token as an enterprise auth boundary.

## Tool Metadata Policy

Tool metadata must match behavior.

- Read-only means no file write, no process execution with write potential, no network publishing, and no external side effects.
- Mutating means source/worktree/artifact changes are possible.
- Destructive means overwrite/delete risk exists.
- Open-world means the tool may reach outside the current account/repo boundary, including network/tunnel/bash behavior.

Every tool descriptor should include `readOnlyHint`, `destructiveHint`, and `openWorldHint` once supported by the protocol layer.

## Path Guard Policy

Path decisions should use resolved real paths, not string prefix checks alone.

Requirements:

- normalize and resolve user-supplied paths;
- require containment under an allowed workspace root;
- reject parent traversal escapes;
- reject symlink escapes;
- block `.git` internals;
- block configured secret globs;
- cap file read sizes;
- detect binary files;
- redact secret-like values in returned snippets;
- test all of the above.

The current implementation ports the CodexPro-style path-guard model into the Python workspace layer: resolved paths must stay under allowed roots, blocked globs are enforced, symlink escapes are rejected, and read/write sizes are capped.

## Secret And Redaction Policy

Do not return or log:

- API keys;
- OAuth tokens;
- Codex auth files;
- `.env` values;
- private keys;
- local session transcripts by default;
- raw prompt bodies by default;
- full Codex stdout/stderr by default.

Returned context should include omission notes so ChatGPT knows when data was intentionally withheld.

## Logging And Artifacts

Audit logs should record:

- timestamp;
- request id;
- tool name;
- workspace id or safe display name;
- status code/result category;
- duration;
- correlation/job id;
- denial reason when applicable.

Audit logs should not record:

- prompt text;
- full file contents;
- full stdout/stderr;
- tokens;
- auth headers;
- connector URLs with query tokens.

Job artifacts may store rawer data only when explicitly enabled. Defaults should store bounded summaries and redacted structured events.

## Bash And Direct Edit Policy

Safe bash is not equivalent to sandboxing. Even commands like `npm test` can execute arbitrary package scripts.

Default:

- no generic bash tool;
- no direct source write/edit tool;
- use `codex_apply_job` worktrees for code changes;
- use `.ai-bridge` for handoff writes.

Optional power mode:

- command allowlist;
- no shell expansion unless full bash is explicitly enabled;
- timeout and output caps;
- working directory must be workspace-contained;
- environment allowlist;
- explicit mutating/open-world annotations.

## Codex Session Policy

Codex session discovery is useful for continuity, but transcripts can contain private source, prompts, credentials, and local paths.

Default:

- `codex_list_sessions` metadata only;
- transcript reads disabled unless `power_tools.codex_session_read` is enabled.

Optional staged behavior:

1. metadata only: timestamp, session id, redacted summary, workspace id;
2. bounded transcript read with redaction and no source path return;
3. full transcript export only through explicit local command, not default MCP.

## Current Verification And Remaining Hardening

The current implementation has addressed the original high-risk connector gaps: public schema validation, public/internal argument translation tests, hidden experimental handler removal, apply-job-only diff retrieval, default log redaction, prompt stdin transport, authenticated tunnel fail-closed behavior, and explicit mutating/open-world annotations for interactive/resume tools.

Verified so far:

- local MCP probe against a disposable repo;
- real Codex CLI `0.141.0` plan job through MCP;
- current Codex JSONL `agent_message` result parsing;
- token-gated local server behavior in automated tests;
- power tools denied by default.

Remaining hardening is future-facing rather than a known boundary break:

- real ChatGPT Developer Mode connection eval;
- real token-gated public tunnel eval;
- real apply-job worktree eval from ChatGPT;
- real resume/interactive continuation eval from ChatGPT;
- stricter or richer ChatGPT tool-card resources;
- CORS policy if a trusted standalone local UI is added;
- OAuth 2.1 if this becomes a multi-user or app-store connector;
- broader Codex CLI compatibility probes across installed versions.

## OpenAI Guidance Used

OpenAI Developer Mode supports streaming HTTP MCP and treats tools without `readOnlyHint` as write actions. Apps SDK guidance expects strong tool metadata, structured content when available, least privilege, server-side validation, logging redaction, and authentication for user-specific data or write actions.
