# Codex MCP Wrapper

`codex-mcp-wrapper` is a local-first Streamable HTTP MCP server for running Codex CLI maintainer workflows from MCP-compatible clients.

It is designed for open-source repository maintenance where scope control, job lifecycle, worktree isolation, diff inspection, and auditability matter.

## Why This Is Not Just `codex mcp-server`

Codex already provides a direct MCP server for conversational Codex use. This project is different: it adds a local maintainer automation layer around Codex CLI.

It focuses on:

- async job lifecycle management;
- configured allowed repository roots;
- read-only analysis by default;
- worktree-isolated apply jobs;
- status, result, and diff inspection APIs;
- review and release workflow integration;
- conservative defaults for local OSS maintenance.

The goal is not to replace Codex's native MCP server. The goal is to make Codex easier to use inside structured maintainer dashboards, local MCP clients, and repository automation workflows where jobs, diffs, scope control, and auditability matter.

## What It Does

- Exposes Codex CLI workflows through a single local `/mcp` endpoint.
- Starts read-only planning jobs.
- Starts isolated apply jobs in git worktrees.
- Returns job status, results, and file diffs.
- Supports review and resume workflows where available.
- Keeps defaults conservative: localhost, explicit repository roots, read-only default, dangerous bypass disabled.

## Architecture

```text
MCP client
   |
   | Streamable HTTP /mcp
   v
codex-mcp-wrapper
   |
   | validates tool name
   | validates repository root
   | applies safety config
   | creates job record
   v
Job manager
   |
   | read-only plan OR isolated apply
   v
Codex CLI subprocess
   |
   | optional git worktree for apply jobs
   v
Result store
   |
   | status / result / diff
   v
MCP client
   |
   | human review
   v
manual merge
```

Safety gates:

- explicit public tools only;
- localhost default;
- allowed roots only;
- read-only default;
- no dangerous bypass by default;
- mutating tools marked non-read-only;
- worktree isolation;
- metadata-only audit logs;
- redacted config/output;
- human diff review.

## Requirements

- Python 3.10+
- A working `codex` CLI on `PATH`
- A Codex/OpenAI login or API key configured for the Codex CLI
- Git for apply-mode worktrees

Install Python dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Edit `config.yaml` before starting the server.

```yaml
server:
  host: 127.0.0.1
  port: 8000
  enable_cors: false

repositories:
  default: /absolute/path/to/owned/repo
  allowed:
    - /absolute/path/to/owned/repo

security:
  require_git_repo: true
  default_sandbox: read-only
  allow_dangerously_bypass: false
  expose_codex_sandbox_tool: false
```

For another project, set both `repositories.default` and `repositories.allowed` to the repo path or to a parent folder that should be accessible to the MCP client.

## Run

```bash
python server.py
```

The server listens on:

```text
http://127.0.0.1:8000/mcp
```

Keep it local unless you fully understand the consequences of exposing Codex execution to another machine.

## Public MCP Tools

The preferred public MCP tool names are Codex-specific and explicit.

| Tool | Purpose | Read-only |
| --- | --- | --- |
| `codex_plan_job` | Start a read-only analysis job | yes |
| `codex_apply_job` | Start an isolated worktree apply job | no |
| `codex_get_status` | Inspect async job state | yes |
| `codex_get_result` | Fetch completed job output | yes |
| `codex_get_diff` | Inspect one file diff from a job | yes |
| `codex_review` | Run Codex review on owned changes | yes |
| `codex_resume` | Resume a prior Codex session | yes |
| `codex_interactive` | Start a Codex exec session | yes |
| `codex_interactive_reply` | Continue a Codex exec session | yes |
| `codex_get_config` | Return redacted config metadata | yes |

Older neutral aliases are retained only for local client compatibility and may be removed later. They are not the advertised public surface.

## Safety Notes

This project wraps a coding agent. Treat it like a local developer tool, not a public web service.

- Do not expose it to the public internet without authentication and network controls.
- Keep `allow_dangerously_bypass: false`.
- Keep CORS disabled unless you are connecting a trusted local UI.
- Prefer `default_sandbox: read-only` for analysis and `workspace-write` only for repos you trust.
- Review diffs before applying or merging generated changes.
- Do not put secrets in prompts or committed config files.
- Child Codex processes receive a restricted environment allowlist.
- `codex_sandbox` is not exposed in the default public MCP tool surface.

Read-only tools never modify files. Mutating tools create isolated worktrees or apply explicit diffs and should require user confirmation in the MCP client.

## Development

Run local checks:

```bash
python -m compileall .
python -m pytest tests -q
```

Manual server smoke test:

```bash
python server.py
python scripts/manual_mcp_smoke.py
```

## Documentation

- [Why this project matters](docs/WHY_THIS_MATTERS.md)
- [Security model](docs/SECURITY_MODEL.md)
- [Threat model](THREAT_MODEL.md)
- [Security review scope](SECURITY_SCOPE.md)
- [API credits plan](docs/API_CREDITS_PLAN.md)
- [OSS roadmap](OSS_ROADMAP.md)

## License

MIT
