# Security

Please do not report security issues by opening public issues that include secrets, connector URLs, exploit details, local paths, or private repository content.

This project is a local developer bridge between ChatGPT, MCP clients, local repositories, and the Codex CLI. Treat it like a tool with local coding-agent power, not like a harmless web widget.

Detailed security references live in [docs/security/](docs/security/), including
the [security model](docs/security/model.md),
[product boundary](docs/security/product-boundary.md),
[review scope](docs/security/review-scope.md), and
[threat model](docs/security/threat-model.md).

## Product Power Boundary

Security controls in this repo are product controls. They are what make it practical to give ChatGPT useful local power:

- auth keeps public tunnel URLs usable without exposing the bridge to anyone who sees the endpoint;
- launch roots and path guards can keep workspace context bounded when the server is started with `--root`;
- blocked globs can keep common secret files out of reads when a narrower profile enables them;
- worktree apply jobs and isolated worker worktrees make larger Codex changes reviewable before merge;
- named worker tools keep normal worker reports free of backend job ids, session ids, raw transcripts, and private paths;
- redacted job state and capped logs make real debugging possible without saving raw prompts by default;
- power tools make direct edit, bash, and transcript reads available only when explicitly enabled.

## Default Posture

- Server binds to `127.0.0.1`.
- CORS is disabled.
- The checked-in local profile is intentionally full-power: `/` allowed root, `danger-full-access`, direct writes, full bash, Codex session reads, and full child-process environment inheritance.
- Starting with `scripts/start.py --root /path/to/repo` narrows the active runtime profile to that root.
- `codex_plan_job` uses the configured sandbox; in the full-power profile it is not read-only.
- `codex_worker_start` and `codex_worker_message` are mutating/open-world: default workers can write in isolated external worktrees, and `workspace_mode: "read_only"` is explicit advisory mode.
- `codex_apply_job` uses isolated git worktrees.
- Dangerous bypass is enabled by config for explicit full-permission runs.
- Direct source write/edit is enabled.
- Bash is enabled in full mode.
- Codex transcript reads are enabled and bounded/redacted.
- Job state is redacted metadata.
- Job stdout/stderr artifacts are redacted and capped.

## Auth And Tunnels

Non-loopback and tunnel modes must fail closed unless `PATCHBAY_HTTP_TOKEN` is set.

Bearer auth is preferred. Query-token URLs exist for ChatGPT connector flows, but they are sensitive:

```text
https://your-tunnel.example/mcp?patchbay_token=<token>
```

Do not paste real tokenized URLs into docs, issues, logs, screenshots, shared chats, or commits.

The launcher writes per-workspace profiles and runtime config under `PATCHBAY_HOME` when set, otherwise under the user's home directory. Blank logging paths resolve under `PATCHBAY_HOME/runtime` or `~/.patchbay/runtime`, so audit logs, job artifacts, and job state do not populate the repository checkout by default. Profiles may remember roots, ports, tunnel mode, public base URL, and power-mode preferences. Token-like keys are stripped before saving.

Public tunnel modes are launcher-managed child processes. PatchBay does not auto-install `cloudflared` or `ngrok`; install and verify provider binaries separately.

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

The local MCP path, real Codex plan job path, and direct tokenized public-tunnel MCP path are verified on disposable repos. Before public release, the project still needs disposable-repo verification for:

- real ChatGPT Developer Mode connection and natural tool selection;
- ChatGPT-hosted file-parameter artifact import from the actual UI;
- real `codex_apply_job` worktree flow from ChatGPT;
- real resume/interactive continuation flow from ChatGPT.
- real named-worker flow from ChatGPT.

Do not describe the project as production-ready until those evals are complete.

## Reporting

When reporting a security issue privately, include:

- affected version or commit;
- whether the server was localhost-only or tunneled;
- whether `PATCHBAY_HTTP_TOKEN` was set;
- the tool call name and high-level arguments;
- redacted logs or minimal reproduction steps.

Do not include secrets, raw prompt bodies, full source files, or real connector tokens.
