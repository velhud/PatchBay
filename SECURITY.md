# Security

Please do not report security issues by opening public issues that include secrets, connector URLs, exploit details, local paths, or private repository content.

This project is a local developer bridge between ChatGPT, MCP clients, local repositories, and the Codex CLI. Treat it like a tool with local coding-agent power, not like a harmless web widget.

## Product Power Boundary

Security controls in this repo are product controls. They are what make it practical to give ChatGPT useful local power:

- auth keeps public tunnel URLs usable without exposing the bridge to anyone who sees the endpoint;
- allowed roots and path guards keep workspace context bounded;
- blocked globs keep common secret files out of normal reads;
- worktree apply jobs make larger Codex changes reviewable before merge;
- redacted job state and capped logs make real debugging possible without saving raw prompts by default;
- power tools make direct edit, bash, and transcript reads available only when explicitly enabled.

## Default Posture

- Server binds to `127.0.0.1`.
- CORS is disabled.
- Repository access is limited to configured allowed roots.
- `codex_plan_job` runs read-only.
- `codex_apply_job` uses isolated git worktrees.
- Dangerous bypass is disabled.
- Direct source write/edit is disabled.
- Bash is disabled.
- Codex transcript reads are disabled.
- Job state is redacted metadata.
- Job stdout/stderr artifacts are redacted and capped.

## Auth And Tunnels

Non-loopback and tunnel modes must fail closed unless `CODEX_MCP_HTTP_TOKEN` is set.

Bearer auth is preferred. Query-token URLs exist for ChatGPT connector flows, but they are sensitive:

```text
https://your-tunnel.example/mcp?codex_mcp_token=<token>
```

Do not paste real tokenized URLs into docs, issues, logs, screenshots, shared chats, or commits.

The launcher writes per-workspace profiles and runtime config under `CODEX_MCP_HOME` when set, otherwise under the user's home directory. Profiles may remember roots, ports, tunnel mode, public base URL, and power-mode preferences. Token-like keys are stripped before saving.

Public tunnel modes are launcher-managed child processes. The wrapper does not auto-install `cloudflared` or `ngrok`; install and verify provider binaries separately.

## Data That Must Not Be Committed

- API keys or OAuth tokens
- Codex auth files
- `.env` files
- private keys
- real tokenized MCP URLs
- job logs from private repos
- local Codex session transcripts
- private prompt bodies
- generated worktrees containing private code

## Known Pre-release Limits

The local MCP path and real Codex plan job path are verified. Before public release, the project still needs disposable-repo verification for:

- real ChatGPT Developer Mode connection;
- token-gated public tunnel flow;
- real `codex_apply_job` worktree flow from ChatGPT;
- real resume/interactive continuation flow from ChatGPT.

Do not describe the project as production-ready until those evals are complete.

## Reporting

When reporting a security issue privately, include:

- affected version or commit;
- whether the server was localhost-only or tunneled;
- whether `CODEX_MCP_HTTP_TOKEN` was set;
- the tool call name and high-level arguments;
- redacted logs or minimal reproduction steps.

Do not include secrets, raw prompt bodies, full source files, or real connector tokens.
