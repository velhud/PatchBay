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
# codex-cli 0.144.1
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
  codex_post_completion_exit_grace_seconds: 2  # cleanup grace after Codex already completed the turn
  codex_post_completion_cleanup_timeout_seconds: 3  # bound wrapper/pipe cleanup after the final report is durable
  stale_running_job_grace_seconds: 600  # restart/stale reconciliation window, not a worker turn timeout
  max_request_bytes: 16777216
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
  # Complete 31-tool Hub. Set v1 explicitly only for a legacy deployment.
  control_plane: v2
  # Legacy V1 JSON state:
  state_file:
  # V2 SQLite state; blank resolves under PATCHBAY_HOME/runtime/hub/:
  state_db:
  # Enable after first setup so a path mistake cannot create a new Hub.
  require_existing_state: false
  # Pin the known Hub id in production; a different database is refused.
  expected_hub_id:
  # Optional explicit path to the validated marker required before opening an
  # older initialized Hub schema. Blank uses the private marker beside state_db.
  pre_migration_backup_marker:
  heartbeat_stale_seconds: 90
  max_events: 1000
  # Optional edge-side resource overrides for virtualized machines. Useful for
  # WSL edges when Windows drive mounts/interop are intentionally disabled and
  # Linux reports the virtual ext4/VHD capacity instead of real host free space.
  edge:
    # Enable after the first Edge journal exists so a missing journal cannot be
    # mistaken for a fresh Edge using the same enrolled generation.
    require_existing_journal: false
    journal_file:
    pre_migration_backup_marker:
    resource_overrides:
      # disk_free_bytes:
      # disk_total_bytes:
      # disk_used_percent:
      # disk_source: windows-host-configured

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
  max_read_bytes: 5000000
  max_write_bytes: 10000000
  max_diff_bytes: 5000000
  max_search_results: 1000
  search_timeout_ms: 30000
  max_search_timeout_ms: 300000
  max_tree_entries: 5000
  max_skill_count: 500
  max_skill_bytes: 200000

power_tools:
  direct_write: true
  bash_mode: "full"
  bash_timeout_ms: 1800000
  bash_max_output_bytes: 5000000
  codex_session_read: true

logging:
  audit_file:
  job_logs_dir:
  job_state_dir:
  private_evidence_dir:
  job_log_max_bytes: 200000
  process_capture_max_bytes: 4000000
  process_event_line_max_bytes: 8000000
  write_raw_job_logs: false
  access_log: false
  private_evidence_log: false
  store_job_prompts: false
  store_mcp_transcripts: false

workers:
  worktree_root: ""
  file_response_max_bytes: 1000000
  heartbeat_fresh_seconds: 120
  heartbeat_quiet_seconds: 600
  status_recommended_poll_seconds: 30
  status_minimum_poll_seconds: 20
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

`audit_file` is compact metadata. `job_logs_dir` stores bounded/redacted Codex
stdout, stderr, and result artifacts. `job_state_dir` stores durable job state
without prompt bodies. `private_evidence_dir` stores optional private evidence:
full job briefs and full MCP request/response transcripts. See
[`runtime-evidence.md`](runtime-evidence.md).

`process_capture_max_bytes` bounds each in-memory stdout/stderr tail used for
result extraction (default 4 MB, hard cap 64 MB). `process_event_line_max_bytes`
bounds one streamed Codex JSONL event (default 8 MB, hard cap 64 MB). A larger
line is fully drained and recorded as oversized rather than passed to the JSON
parser, so a verbose command cannot deadlock the worker pipe or grow PatchBay
memory without bound. These settings do not limit Codex work, repository files,
or the worker report schema.

For public/default use, leave `private_evidence_log`, `store_job_prompts`, and
`store_mcp_transcripts` false. For a trusted personal VM/workbench where
debuggability matters more than minimizing private local artifacts, set
`private_evidence_log: true`.

Hub commands select the complete V2 31-tool manager surface by default.
`hub.control_plane: v1` explicitly selects the legacy runtime; any other value
is rejected. Blank
`hub.state_db` resolves under `PATCHBAY_HOME/runtime/hub/`; `hub.state_file` is
the legacy V1 JSON store. Hub state is private runtime state for enrolled
machines, durable work groups, current group selections, command routing,
worker briefs needed for durable dispatch/replay, and compact event history. It
is not repository data and should not be committed. If Hub state is corrupt,
PatchBay quarantines it and returns a recovery error instead of silently
resetting fleet/group state.

After initial Hub creation, record the returned Hub id as `hub.expected_hub_id`
and set `hub.require_existing_state: true`. After initial Edge journal creation,
set `hub.edge.require_existing_journal: true` on that Edge. These deployment
continuity guards turn a missing path, wrong mount, or wrong state database into
a startup failure instead of an apparently healthy empty control plane.

`hub.recovery_dispatch_interval_seconds` (default `1.0`, bounded to
`0.1..60`) controls the Hub process's crash-recovery dispatch loop.
`hub.recovery_dispatch_batch_size` (default `100`, bounded to `1..1000`)
limits one recovery cycle. The loop reoffers only durable operations that are
still dispatchable. Normal read/status tools never sweep unrelated mutation
backlogs; a remote read may dispatch only the operation it created itself.
During an online Hub backup, the shared admission gate pauses new Edge claims
and mutation dispatch while result upload, reconciliation, and read/status
traffic remain available.

An initialized Hub database with an older supported schema cannot migrate merely
because a newer process opened it. Startup requires a validated pre-migration
backup marker bound to that exact source state. Create the offline first-upgrade
bundle with `patchbay hub backup create --prepare-migration`; the default marker
is private and adjacent to `hub.state_db`. Set
`hub.pre_migration_backup_marker` only when an operator deliberately stores the
marker elsewhere.

When PatchBay is run by a service manager such as systemd, set a real user home
for the service process. Codex workers inherit `CODEX_HOME` for Codex auth and
PatchBay also passes `HOME`, `XDG_CONFIG_HOME`, and `GIT_CONFIG_GLOBAL` to
worker jobs so tools such as `gh` and `git` can find the same GitHub CLI and git
credential configuration that works in an interactive shell.

```env
HOME=/root
XDG_CONFIG_HOME=/root/.config
GIT_CONFIG_GLOBAL=/root/.gitconfig
CODEX_HOME=/root/.codex
```

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

PatchBay keeps local paths, raw session ids, worker worktree paths, process logs, and runtime files behind the local control boundary unless a specific public tool is designed to expose a bounded summary. Private evidence files can include full ChatGPT tool calls and full worker/Codex prompts when explicitly enabled; they are private runtime evidence, not repository data.
