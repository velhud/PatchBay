# Configuration Reference

This page holds the configuration and launch details that were intentionally moved out of the root README. The README should stay product-first; this page is the operator reference.

## Requirements

- Python 3.10+
- Git
- `codex` CLI on `PATH`
- Codex CLI login or API key configured for the local Codex CLI

Recommended Codex CLI baseline for the current branch:

```bash
codex --version
# codex-cli 0.142.2
```

Install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e ".[test]"  # needed for local test runs
```

`requirements.txt` holds the minimal runtime dependency set. `pyproject.toml` holds package metadata, console entry points (`patchbay` and `patchbay-stdio`), and the `test` extra used by CI and local verification.

## Important defaults

Edit `config.yaml` or use `patchbay start --root ...` to generate a private runtime config.

```yaml
server:
  host: 127.0.0.1
  port: 8000
  max_concurrent_jobs: 10
  queue_enabled: true
  job_timeout_seconds: 0  # 0/none/unlimited disables Codex turn timeout
  codex_session_start_timeout_seconds: 180  # fail only when no Codex JSON session appears after process start
  stale_running_job_grace_seconds: 600  # restart/stale reconciliation window, not a worker turn timeout
  max_request_bytes: 1048576
  enable_cors: false

app:
  tool_mode: worker
  widget_domain: https://web-sandbox.oaiusercontent.com

auth:
  enabled: false
  token_env: PATCHBAY_HTTP_TOKEN
  allow_query_token: true
  require_for_non_loopback: true
  require_for_tunnel: true
  tunnel_mode: none

ownership:
  scope: token

hub:
  state_file:
  heartbeat_stale_seconds: 90
  max_events: 1000

repositories:
  default: /
  allowed:
    - /
  # Optional: folders that codex_list_workspaces may scan shallowly for known
  # repositories when ChatGPT knows a repo name but not the exact path.
  discovery_roots: []
  max_discovery_depth: 3
  max_discovery_results: 50

security:
  require_git_repo: false
  "default_sandbox": danger-full-access
  allow_dangerously_bypass: true
  allowed_env_keys:
    - "*"
  search_timeout_ms: 10000
  max_search_timeout_ms: 60000

power_tools:
  direct_write: true
  bash_mode: "full"
  codex_session_read: true

logging:
  audit_file:
  job_logs_dir:
  job_state_dir:
  write_raw_job_logs: false
  access_log: false

workers:
  worktree_root: ""
  file_response_max_bytes: 25000
  heartbeat_fresh_seconds: 120
  heartbeat_quiet_seconds: 600
  status_recommended_poll_seconds: 20
  status_minimum_poll_seconds: 10
  stop_artifact_wait_seconds: 2

pro_requests:
  root:
  mirror_enabled: true
  mirror_dir: ".ai-bridge/pro-requests"
  max_report_bytes: 200000
  max_response_bytes: 200000
  max_attachment_bytes: 2000000
  max_attachments_per_request: 10
```

Blank logging paths resolve outside the checkout under `PATCHBAY_HOME/runtime` when `PATCHBAY_HOME` is set, otherwise under `~/.patchbay/runtime`. Set explicit paths only when you deliberately want repo-local or custom runtime state.

Blank `hub.state_file` resolves to `PATCHBAY_HOME/runtime/hub/hub-state.json`.
Hub state is private runtime state for enrolled machines, command routing, and
compact event history. It is not repository data and should not be committed.

## Authentication

For local loopback use, auth can remain off. For non-loopback bind addresses, public URL mode, tunnel mode, or explicit `PATCHBAY_HTTP_TOKEN`, every MCP/status request must include a matching Bearer token or an allowed query token.

Prefer Bearer auth where the client supports headers:

```http
Authorization: Bearer <token>
```

Copied ChatGPT Server URLs can use query-token auth:

```text
https://your-tunnel.example/mcp?patchbay_token=<token>
```

Never commit or share a real tokenized URL.

## Tool mode defaults

Use `--tool-mode worker` for the first ChatGPT validation run. It exposes the worker tools plus the read-only context tools needed to brief them, while hiding low-level job/session controls and aliases.

ChatGPT can inspect mode choices with `codex_tool_mode_info` and request a session-local mode change with `codex_tool_mode_switch`. The switch does not rewrite config files.

## Runtime state

PatchBay keeps local paths, raw session ids, worker worktree paths, process logs, and runtime files behind the local control boundary unless a specific public tool is designed to expose a bounded summary.
