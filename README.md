# Codex MCP Wrapper

A small Streamable HTTP MCP server that lets MCP-compatible clients start and inspect Codex CLI jobs.

This repository is a cleaned open-source release of an older local prototype. It is intentionally conservative by default: it binds to localhost, only allows configured repository roots, uses a read-only Codex sandbox unless configured otherwise, and refuses the Codex dangerous bypass flag unless explicitly enabled in `config.yaml`.

## What It Does

- Exposes Codex CLI workflows through a single `/mcp` endpoint.
- Starts read-only planning jobs and isolated apply jobs.
- Creates git worktrees for apply jobs so changes do not land directly in the original checkout.
- Returns job status, results, and file diffs through MCP tools.
- Supports Codex resume/review/cloud helper commands when your installed Codex CLI supports them.

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
  max_concurrent_jobs: 1

repositories:
  default: .
  allowed:
    - .

security:
  require_git_repo: true
  default_sandbox: read-only
  allow_dangerously_bypass: false
```

For another project, set both `repositories.default` and `repositories.allowed` to the repo path or to a parent folder that should be allowed.

## Run

```bash
python server.py
```

The server listens on:

```text
http://127.0.0.1:8000/mcp
```

Keep it local unless you fully understand the consequences of exposing Codex execution to another machine.

## MCP Tools

The server currently exposes neutral tool names for client compatibility. Internally they map to Codex actions:

| MCP tool | Internal action |
| --- | --- |
| `query_text_analytics` | `codex_plan_job` |
| `update_content_record` | `codex_apply_job` |
| `check_operation_status` | `codex_get_status` |
| `fetch_operation_result` | `codex_get_result` |
| `fetch_record_delta` | `codex_get_diff` |
| `analyze_content_changes` | `codex_review` |
| `continue_session` | `codex_resume` |
| `start_conversational_query` | `codex_interactive` |
| `continue_conversational_query` | `codex_interactive_reply` |
| `get_system_config` | `codex_get_config` |

## Safety Notes

This project wraps a coding agent. Treat it like a local developer tool, not a public web service.

- Do not expose it to the public internet without authentication and network controls.
- Keep `allow_dangerously_bypass: false` unless you are working in a disposable environment.
- Prefer `default_sandbox: read-only` for analysis and `workspace-write` only for repos you trust.
- Review diffs before applying or merging generated changes.
- Do not put secrets in prompts or committed config files.

## Development

Run the lightweight protocol smoke test:

```bash
python test_mcp.py
```

The test expects the server to be running locally.

## License

MIT
