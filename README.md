<h1 align="center">PatchBay</h1>

<p align="center">
  <strong>Route ChatGPT's context into local Codex workers.</strong>
</p>

<p align="center">
  <img alt="Status: pre-release verified" src="https://img.shields.io/badge/status-pre--release%20verified-orange">
  <img alt="MCP: Streamable HTTP and stdio" src="https://img.shields.io/badge/MCP-HTTP%20%2B%20stdio-blue">
  <img alt="Runtime: Python and FastAPI" src="https://img.shields.io/badge/runtime-Python%20%2B%20FastAPI-3776AB">
  <img alt="Codex CLI baseline: 0.142.2" src="https://img.shields.io/badge/Codex%20CLI-0.142.2-black">
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green">
</p>

PatchBay is a local MCP control plane that routes ChatGPT's active conversation, project context, generated files, and long-running reasoning into your local Codex CLI. It lets ChatGPT open approved repositories, brief durable Codex workers by name, pass reports between them, inspect diffs, and apply accepted work from the chat instead of copy-pasting prompts, files, diffs, and status notes between ChatGPT and terminal Codex.

Use it when the best context already lives in ChatGPT web/Pro, Projects, memory, or another conversation, but the real work needs your local repository, local Codex setup, git state, tools, and execution environment.

| What PatchBay makes possible | Why it matters |
| --- | --- |
| ChatGPT context becomes executable | Reuse a deep ChatGPT conversation, project instructions, memory, and generated files as source material for local Codex work. |
| No copy-paste bridge | Move briefs, artifacts, reports, diffs, and follow-up instructions through MCP instead of manually shuttling text between apps. |
| Durable local worker loops | Start named Codex workers, continue them after restart, pass context between workers, and inspect their reports or diffs from ChatGPT. |
| Reverse Pro escalation loop | Package a local blocked problem for ChatGPT Pro, store the answer durably, then explicitly dispatch it to an idle origin worker or a new isolated worker. |
| Local execution stays local | Codex still runs on your machine against your repo, git state, toolchain, and configured Codex account. |
| Explicit power boundary | Isolated worktrees, tool modes, tokens, metadata, and integration previews keep powerful actions visible and reviewable. |

## Contents

- [Current Readiness](#current-readiness)
- [Capabilities](#capabilities)
- [Architecture](#architecture)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Public MCP Tool Tiers](#public-mcp-tool-tiers)
- [Power Boundary And Controls](#power-boundary-and-controls)
- [Development And Verification](#development-and-verification)

## Current Readiness

This branch is **pre-release verified**, not public-release complete.

| Area | Status |
| --- | --- |
| Codex CLI baseline | Current local verification recorded `codex-cli 0.142.2` |
| Python checks | `compileall` passes |
| Test suite | `281` tests pass |
| Live local MCP probe | `scripts/live_mcp_eval.py --json` passes against a disposable repo |
| Pro Escalation request loop | Unit tests and the live MCP probe cover CLI create, MCP list/read/claim/respond, CLI response readback, and blocked origin-worker dispatch |
| Named worker continuity eval | `scripts/worker_phase1_eval.py --timeout 600` passes real Codex start/restart/continue |
| Isolated writing worker eval | `scripts/worker_phase2_eval.py --timeout 900` passes real Codex isolated write/restart/continue/diff/cleanup |
| Multi-worker coordination eval | `scripts/worker_phase3_eval.py --timeout 900` passes real Codex peer diff/report relay |
| Worker integration eval | `scripts/worker_phase4_eval.py --timeout 900` passes real Codex integration preview/apply |
| Real MCP worker negative-case trial | `scripts/real_mcp_worker_trial.py --include-safety-cases` passes direct MCP worker lifecycle and negative cases |
| Direct multi-client MCP trial | `scripts/real_mcp_worker_trial.py --multi-client --tool-mode worker` passes two-session tool-mode, ownership, takeover, preview, and integration checks |
| Public tunnel MCP probe | Earlier tokenized ngrok MCP simulator passed health, `initialize`, worker-mode `tools/list`, artifact inbox import/list/inspect, isolated worker artifact attachment/read, integration exclusion, and cleanup; current run blocked only because no validation ngrok hostname was provided |
| Real Codex through MCP | `codex_plan_job` completes through PatchBay |
| Current Codex JSONL parsing | `agent_message` results parse into structured output |
| Active ChatGPT Pro VM worker use | Working reliably in current internal use for ChatGPT Pro to a private PatchBay VM managing local Codex workers; occasional small bugs are still expected |
| Parallel ChatGPT browser conversations | Pending; multiple independent ChatGPT browser conversations sharing one Server URL have not yet been tried |
| Real apply-job diff eval from ChatGPT | Pending |
| Real resume/continuation eval from ChatGPT | Pending |

## Capabilities

| Capability | Included |
| --- | --- |
| Streamable HTTP MCP endpoint | `/mcp` |
| Stdio MCP transport | `patchbay stdio` or `patchbay-stdio` for local MCP hosts |
| ChatGPT-ready descriptors | tool annotations, `_meta`, security schemes, invocation labels |
| Apps-style result card | compact passive `text/html;profile=mcp-app` receipt for PatchBay tool results |
| Workspace context | tree, read, search, git status/diff, AGENTS, skills, context packs |
| Codex orchestration | plan, apply, status, result, diff, cancel, review, interactive, resume |
| Durable worker facade | discover worker model/reasoning options, import generated artifact context, start/message/list/inspect/stop named Codex colleagues, use isolated writing worktrees by default, and include bounded peer-worker context |
| Pro Escalation requests | create local-to-ChatGPT blocked-problem requests, read/claim/respond through MCP, and explicitly dispatch stored answers to workers |
| Repository boundary | allowed roots, path guard, blocked globs, worktree apply jobs |
| Handoff | `.ai-bridge` plan/status/diff and local execute/watch scripts |
| Power modes | direct write, exact edit, safe/full bash, bounded transcript reads |
| Connector UX | installable `patchbay` CLI, doctor, setup, start, settings, stdio, guided setup output, profiles, redacted runtime metadata, token-gated tunnels |

## Architecture

```mermaid
flowchart TD
    A["ChatGPT web/Pro<br/>Developer Mode connector"] -->|"HTTPS /mcp<br/>tokenized Server URL"| B["PatchBay FastAPI server"]
    A2["Local MCP client"] -->|"http://127.0.0.1:8000/mcp"| B

    B --> C["Transport boundary<br/>auth, request caps, MCP sessions, client_ref"]
    C --> D["MCP protocol surface<br/>initialize, tools/list, resources/read, schemas, annotations"]
    D --> E["Session-local tool modes<br/>worker, standard, full, minimal + aliases"]
    D -. "optional app.tool_cards=true" .-> R["Passive Apps receipt widget<br/>ui://widget/patchbay-tool-card-v2.html"]

    E --> H["Tool handler"]
    H --> W["Workspace context<br/>allowed roots, path guard, tree/read/search, git, AGENTS, skills"]
    H --> N["Worker runtime<br/>named workers, model/reasoning menu, peer context, inspect, stop"]
    H --> I["Artifact inbox<br/>ChatGPT file/zip import, manifest, materialize into worker"]
    H --> J["Job manager/executor<br/>durable jobs, Codex command builder, JSONL parsing, cancellation"]
    H --> P["Power tools<br/>direct write/edit, command, transcript read"]
    H --> L["Repo mutation locks<br/>repo_busy instead of hidden base-write queues"]

    N --> WT["Isolated/shared/read-only worker worktrees<br/>changes, file reads, diffs, integration preview/apply"]
    I --> WT
    J --> X["Local Codex CLI subprocesses"]
    P --> Y["Local shell/files/session logs"]
    W --> Z["Approved repositories<br/>.ai-bridge handoffs and context packs"]
    WT --> Z
    X --> S["PatchBay runtime state<br/>profiles, runtime config/status, job records, artifacts, locks"]
```

The core runtime is Python/FastAPI. ChatGPT sees the MCP surface; PatchBay keeps local paths, raw session ids, worker worktree paths, process logs, and runtime files behind the local control boundary unless a specific public tool is designed to expose a bounded summary.

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

`requirements.txt` holds the minimal runtime dependency set.
`pyproject.toml` holds package metadata, console entry points (`patchbay` and
`patchbay-stdio`), and the `test` extra used by CI and local verification.

## Quick Start

Start with a disposable git repo, not a private production checkout:

```bash
tmpdir=$(mktemp -d)
mkdir -p "$tmpdir/repo"
cd "$tmpdir/repo"
git init
printf '# Disposable Eval\n' > README.md
git add README.md
git -c user.name='Eval User' -c user.email='eval@example.invalid' commit -m init
```

Check connector readiness without opening a public tunnel:

```bash
patchbay doctor
patchbay start --root "$tmpdir/repo" --tool-mode worker --print-only
patchbay start --root "$tmpdir/repo" --tool-mode worker --print-only --json
```

`patchbay start --print-only` prints a ChatGPT setup guide with the Server URL,
authentication choice, tool mode, tunnel mode, exact ChatGPT Developer Mode
steps, useful restart/profile commands, and token/tunnel warnings. The JSON
form returns the same data under `setup_guide`, so local wrappers can display
the connector steps without scraping terminal text. `python scripts/start.py`
and `python scripts/doctor.py` remain compatibility wrappers.

For local MCP clients, start the local MCP server:

```bash
patchbay start --root "$tmpdir/repo" --tool-mode worker --save-profile
```

The local endpoint is:

```text
http://127.0.0.1:8000/mcp
```

For local MCP hosts that prefer stdio instead of HTTP:

```bash
patchbay stdio --config config.yaml
# or, after package installation:
patchbay-stdio --config config.yaml
```

For ChatGPT web, start PatchBay with an HTTPS tunnel and worker-first tool surface. Tunnel startup fails closed without a token:

```bash
export PATCHBAY_HTTP_TOKEN='<long-random-token>'
patchbay start \
  --root "$tmpdir/repo" \
  --tunnel-mode cloudflare \
  --tool-mode worker \
  --save-profile \
  --reveal-token
```

Copy the full tokenized Server URL printed by `--reveal-token`. It should look like `https://.../mcp?patchbay_token=...`. Tokenized ChatGPT Server URLs are redacted unless you ask to reveal them. To install Cloudflare Tunnel into PatchBay's local bin directory, run `patchbay install-cloudflared` explicitly. PatchBay also exposes tunnel shortcuts:

```bash
patchbay ngrok --root "$tmpdir/repo" --hostname your-domain.ngrok-free.dev --tool-mode worker --reveal-token
patchbay stable --root "$tmpdir/repo" --hostname patchbay.example.com --tunnel-name patchbay --tool-mode worker --reveal-token
```

OpenAI's Apps SDK docs describe the same connector shape: enable Developer Mode, create a connector, paste an HTTPS `/mcp` URL, then open a new chat and add the connector from the `+` / More menu. See [OpenAI Apps SDK quickstart](https://developers.openai.com/apps-sdk/quickstart#add-your-app-to-chatgpt) and [Connect from ChatGPT](https://developers.openai.com/apps-sdk/deploy/connect-chatgpt#create-a-connector).

Use `--tool-mode worker` for the first ChatGPT validation run; it exposes the worker tools plus the read-only context tools needed to brief them, while hiding low-level job/session controls and aliases. Direct tokenized public-tunnel MCP simulation has passed through ngrok, including artifact inbox transfer into an isolated worker. The active internal ChatGPT Pro to private VM worker loop is now working well enough for regular PatchBay self-use, but formal public-release validation still needs recorded ChatGPT UI/tool-selection evidence, especially for multiple independent browser conversations sharing one Server URL.

One copied Server URL points to one shared local server. Multiple ChatGPT conversations or MCP clients connected to that URL can see the same local worker, job, artifact, and repository state. Start each conversation with `codex_self_test`; it returns a session-relative `client_ref`, active MCP session count, shared-server coordination note, and readiness checks for the command environment (`codex`, `git`, `bash`, `rg`, and `python3`) without returning raw MCP session ids.

For multi-repository validation, include every repository at launch time. `--root` sets the default workspace and narrows `repositories.allowed` to that root unless extra roots are supplied:

```bash
patchbay start \
  --root "$repo_a" \
  --allow-root "$repo_b" \
  --tunnel-mode cloudflare \
  --tool-mode worker \
  --reveal-token
```

If a tool reports that a path is outside configured allowed roots, treat it as a launcher setup issue. Restart PatchBay with the missing repository passed through `--allow-root` or add it to `repositories.allowed`; do not work around the path guard.

ChatGPT can inspect mode choices with `codex_tool_mode_info` and request a session-local mode change with `codex_tool_mode_switch`. The switch does not rewrite config files. Direct MCP clients that call `tools/list` again on the same MCP session will see the new catalog; other sessions keep their own effective mode. ChatGPT Developer Mode may require refreshing the connector metadata before newly exposed tools appear.

Create the ChatGPT connector/app with:

```text
Settings -> Apps & Connectors -> Advanced settings
Developer mode: on
Enforce CSP in developer mode: on
Settings -> Connectors -> Create

Name: PatchBay
Description: Route ChatGPT context into local Codex workers
Connector URL / Server URL: paste the full HTTPS /mcp URL printed by patchbay start --reveal-token
Authentication: No Authentication / None
```

The ChatGPT app auth setting is `No Authentication / None` because the Server URL already includes the private PatchBay token. Do not configure OAuth or paste an API key into ChatGPT for this local bridge.

After ChatGPT shows the advertised tools, open a new chat, add PatchBay from the `+` / More menu, and start with:

```text
Use PatchBay. Act as the manager of local Codex workers, not as the primary file reader. Call codex_self_test, then codex_open_workspace, then tell me what repo you can see, which worker tools are available, and how you would split a non-trivial task across workers.
```

See [QUICKSTART.md](QUICKSTART.md) for the full disposable-repo flow.

## Configuration

Edit `config.yaml` or use `patchbay start --root ...` to generate a private runtime config.

Important defaults:

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

Blank logging paths resolve outside the checkout under `PATCHBAY_HOME/runtime`
when `PATCHBAY_HOME` is set, otherwise under `~/.patchbay/runtime`.
Set explicit paths only when you deliberately want repo-local or custom runtime
state.

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

## Public MCP Tool Tiers

The canonical public names are `codex_*`. In `full` tool mode, compatibility aliases such as `read`, `write`, `edit`, `bash`, `show_changes`, `git_status`, `git_diff`, `workspace_snapshot`, `export_pro_context`, and `handoff_to_agent` can also be advertised. Aliases resolve to the canonical handlers and now expose precise CodexPro-derived input schemas adapted to PatchBay argument names. Use `--tool-mode worker` for a worker-first surface that hides low-level job/session controls and compatibility aliases while keeping worker tools plus the context tools needed to brief them. In this mode, ChatGPT should act as a manager and engineering lead: for non-trivial repository, Documents, codebase, architecture, audit, debugging, implementation, or review work, appoint named Codex workers through natural-language briefs and synthesize their reports. Direct read/search tools remain available for orientation, briefing context, focused verification, exact line/diff checks, reviewing worker evidence, specific doubts, or tiny tasks where a worker would be unnecessary. They should not become the main execution loop for broad work. Repeated direct read/search calls are a sign that ChatGPT is doing line-worker analysis and should start or continue a worker instead. All modes expose `codex_tool_mode_info` and `codex_tool_mode_switch` so ChatGPT can compare surfaces and request temporary session-local broadening when the host refreshes the tool list.

### Natural-language workers

| Tool | Purpose | Read-only |
| --- | --- | --- |
| `codex_worker_options` | Return a bounded Codex model/reasoning menu for worker setup without exposing raw config/catalog data | yes |
| `codex_worker_inbox` | Import ChatGPT-generated files or zips into local artifact context, list/inspect them, or clean up local copies | no |
| `codex_worker_start` | Start a named Codex colleague with an English brief; defaults to an isolated writing worktree | no |
| `codex_worker_message` | Continue or redirect the same Codex conversation by worker name in the same workspace | no |
| `codex_worker_list` | List current-scope workers with compact `team_status`, liveness lines, checkpoints, latest report, and hidden-history count | yes |
| `codex_worker_status` | Show the compact pull-based status bar for the current work run plus live/problem workers | yes |
| `codex_worker_wait` | Wait once, then return a fresh compact worker status without rapid polling | yes |
| `codex_worker_inspect` | Read one worker's compact status, current state, report, changed files, worker-created file content, one-file diff, or integration preview | yes |
| `codex_worker_integrate` | Apply an explicitly accepted isolated worker result to the base checkout without committing or deleting the worktree | no |
| `codex_worker_stop` | Stop the active turn and optionally discard an isolated worker workspace | no |

Workers are derived from persisted job records and Codex sessions. Human worker names are scoped to the base workspace, so `Small Implementer` can exist in more than one repo; pass `repo_path` or use the public `worker_id` only when a name is ambiguous. ChatGPT should treat these workers as local assistants, not low-level commands: ask natural questions, assign goals and deliverables, and let workers find the relevant repository details unless exact paths matter. Workers are continuing specialists, not disposable one-shot summaries; if a report is thin, contradictory, missing evidence, missing validation, or important enough to drive a decision, ChatGPT should continue the same worker with `codex_worker_message` before final synthesis. Worker result reports are expected to include a concise `summary`, substantive `detailed_report`, concrete `evidence`, changed files, commands/tests, notes, risks, open questions, and next steps; PatchBay surfaces those fields in the public report instead of reducing them to a one-line summary. For larger work, ChatGPT should consider a team rather than one shallow worker: source/folder investigators, implementation owners, reviewers, verification workers, and a synthesis worker using `context_from_workers`. Up to 10 concurrent worker slots may be available depending on server config, and using them is intended when briefs are clear. Consequential writable audits and implementation tasks should ask workers for a durable report file or changed-file evidence in the worker workspace, then inspect that evidence before integration. Read-only workers keep the source checkout read-only; their evidence is exposed as PatchBay-managed structured reports, partial reports, live checkpoints, `latest_partial_note`, and `report_artifacts`; an empty repo report-file list is explained as read-only state, not failure. When ChatGPT needs control over the underlying Codex model or reasoning depth, it should call `codex_worker_options` and then pass `model` and/or `reasoning_effort` to `codex_worker_start`. `repo_path` is accepted on `codex_worker_options` as a harmless compatibility field, but it is ignored because the model menu is runtime metadata rather than repository state. The model ladder is advisory: Spark is the default for compact small workers because it is fast and effectively free; GPT-5.4 Mini is the small reliable alternative; GPT-5.4 is the main serious worker for normal above-average tasks; GPT-5.5 is the highest-authority lane for innovation, creative architecture, unresolved problems, sensitive/final judgment, and unusually hard synthesis. When ChatGPT has generated a plan, file, or zip that local Codex should use, it should call `codex_worker_inbox(action="import_file")` and pass the returned artifact id through `context_from_artifacts`. Imports are local context only and can be repeated; they do not edit the repository. Follow-up `codex_worker_message` calls inherit the worker's prior model/reasoning choices unless explicitly overridden and can attach later imported artifacts.

Default writing workers use durable external worktrees with on-demand changed-file, paged file-content, and one-file diff inspection. Before integration, `codex_read_file` reads only the base checkout; its `max_bytes` caps the returned page, not the whole file, and large base reads may return `next_start_line`. Pagination and byte caps are transport/result-stability controls, not a directive to save tokens or avoid necessary evidence. Use `codex_worker_inspect(view="file", file_path="...")` to read a worker-created file from its isolated worktree. File views include `start_line`, `end_line`, `next_start_line`, `max_bytes_applied`, and worker-report location metadata so large reports can be read in chunks without pulling a whole file into one ChatGPT tool result. Imported artifacts are copied into `.ai-bridge/imported-artifacts/` inside the isolated worker worktree and excluded from changes, diffs, integration previews, and applies. Worker start/message calls can include bounded `report`, `changes`, `diff`, or `review` context from other workers; use `review` when another worker needs report plus changed-file inventory plus bounded diff for review before integration. `codex_worker_start(auto_suffix=true)` can rerun a phase with a reused human name, and `include_untracked_from_base` can copy selected accepted untracked base files into a new isolated worker. Unchanged copied baseline files are treated as context and excluded from integration patches; if the worker edits one of those copied baseline files, integration preview reports `modified_included_untracked_base_files` and blocks automatic apply so ChatGPT can ask for a separate patch, integrate manually, or commit/track the base context first. `codex_worker_list` returns concise `team_status` plus `team_report`. `codex_worker_status` returns only the compact status bar: counts, deltas since the last check for the same work run/conversation owner, suggested action, one short line per worker, and polling guidance. Default `scope=current` is deliberately not the full archive: it shows the current work run plus live/problem workers and reports how many old completed/stopped workers are hidden. Use `scope=conversation` to intentionally reuse earlier workers from the same ChatGPT conversation, `scope=recent` for recently active workers, and `scope=history` only when the durable archive is needed. If `repo_path` is omitted, worker list/status/wait cover all allowed repositories so active work is not hidden by the default workspace; pass `repo_path` when deliberately narrowing to one repo. For ordinary monitoring, ChatGPT should wait about 20-30 seconds between status calls and follow `recommended_next_poll_seconds`; polling every few seconds is reserved for explicit near-real-time requests or immediate recovery from a lost/failed status. If ChatGPT calls status too soon, PatchBay returns a cached `poll_too_early: true` response with `status_current: false` and `retry_after_seconds` without resetting activity deltas. `codex_worker_wait` is the preferred patient path: it waits once, raises too-small `wait_seconds` values to the configured minimum cadence, then returns a fresh compact status without interrupting workers or exposing raw logs. Compact status intentionally omits raw shell command text; use `codex_worker_inspect(view="status")` only for deliberate debugging when the actual command preview matters. Worker lists can be narrowed further with `active_only`, `include_stopped=false`, `owned_only`, or `created_after`. Before final synthesis on substantial work, ChatGPT should check the relevant worker status and either stop/supersede stale unneeded workers or explicitly report that a worker remains active and why.

PatchBay streams Codex JSON events while a worker turn is running. When Codex emits `thread.started`, the worker's session is recorded immediately rather than only after completion, and full status views expose bounded lifecycle diagnostics such as process pid, launch/process timestamps, last event, phase, event count, stdout/stderr bytes seen, command preview, progress, heartbeat age, exit code, session-created status, and classified failure categories when Codex fails before useful work. Useful `agent_message` events become bounded manager-level checkpoints under `latest_checkpoints` and the latest short partial note under `latest_partial_note`; `liveness.status` uses the compact manager categories `starting`, `active`, `quiet`, `stale`, `lost`, `completed`, `failed`, and `cancelled`. Terminal jobs clear live-only command fields, so completed/failed/cancelled turns do not keep showing an old `current_command_preview`. The freshness/quiet display windows are configurable through `workers.heartbeat_fresh_seconds` and `workers.heartbeat_quiet_seconds`; status polling guidance is separately configurable through `workers.status_recommended_poll_seconds` and `workers.status_minimum_poll_seconds`. A missing final report is not automatically a stuck worker. PatchBay persists a result artifact even when Codex does not emit the final structured result event; it falls back to the latest agent message, a bounded raw-output note/preview, or a redacted failure diagnostic so cancelled, failed, and unusual turns still have manager-readable evidence. Use `codex_worker_status` and `codex_worker_wait` to compare activity deltas before stopping, at the returned cadence rather than as a rapid loop. Stopping a worker preserves captured partial reports and checkpoints, and `workers.stop_artifact_wait_seconds` lets the stop response briefly wait for already-captured evidence to attach, but it still interrupts the active turn. `job_timeout_seconds: 0` keeps long worker turns unlimited, while `codex_session_start_timeout_seconds` only fails a process that started but never produced a Codex JSON session. Persisted running jobs survive PatchBay restart as recovered-running records and are reconciled only after grace checks and live-runtime checks, including tracked executor tasks, tracked subprocesses, live process pids, and recent heartbeats. Worker integration normally refuses a dirty base, but `accepted_dirty_base` can name known phase artifacts while still blocking unexpected local changes; `allow_dirty_base` remains the expert override.

If multiple ChatGPT conversations share one Server URL, worker and artifact views include owner-relative coordination flags. By default `ownership.scope: token` treats calls using the same bearer/query token as the same coordination owner, so short-lived transport sessions from the same copied connector URL normally continue the same workers without takeover. When ChatGPT supplies `_meta["openai/session"]`, PatchBay hashes it into `chatgpt_session_ref` and stamps workers with a separate `work_run_ref`; raw OpenAI metadata is not logged or returned. `active_mcp_sessions` is transport-session churn, not proof of worker ownership or conversation identity by itself. Public ownership statuses distinguish `current_client`, `legacy_connection` for older unscoped records, `other_token_owner` for records created under a different tokenized URL, `different_owner_scope` for records created under a different configured owner mode, and `other_connection` for same-scope non-token owner differences. `legacy_connection` does not prove a different ChatGPT owner; it means the old record did not store enough scope metadata to know. Read/list/inspect remain shared, but mutating another owner's worker or artifact requires an explicit `takeover: true` call after user confirmation. A successful takeover rewrites owner metadata with the current scoped owner model. When `queue_enabled: true`, Codex turns above `max_concurrent_jobs` remain pending until an execution slot opens. `codex_startup_serialization_enabled: true` adds a narrower gate: only Codex auth/session startup is serialized per effective Codex home, using both an in-process gate and a host file lock, then full worker turns continue concurrently after session creation. PatchBay also passes that resolved home to `codex` as `CODEX_HOME`, so the gate, session reader, model menu, and spawned CLI use the same auth/session directory. This protects rotating Codex login tokens without turning the worker system into a single-worker queue. Base-checkout mutation paths, including direct writes, command execution, shared-write workers, and worker integration, still use per-repository mutation locks and return `repo_busy` instead of queueing hidden writes. PatchBay does not add a worker database, message bus, transcript copy, role engine, automatic reviewer chain, automatic commits, or automatic merge queue.

### Pro Escalation requests

| Tool | Purpose | Read-only |
| --- | --- | --- |
| `codex_pro_request_list` | List open or recent local-to-ChatGPT Pro requests | yes |
| `codex_pro_request_read` | Read one bounded report, response, attachment index, and repo staleness check | yes |
| `codex_pro_request_claim` | Claim the request for the current MCP connection | no |
| `codex_pro_request_respond` | Store ChatGPT Pro's answer only; no execution, dispatch, edit, apply, or commit | no |
| `codex_pro_request_dispatch` | Explicitly send the stored answer to an idle origin worker or start a new isolated worker | no |
| `codex_pro_request_close` | Close, cancel, or supersede a request | no |

Local creation and operator inspection use `patchbay pro-request create/list/show/response/dispatch/close`. The canonical store lives in PatchBay runtime storage; `.ai-bridge/pro-requests/<request-id>/` is a sanitized mirror for local visibility. Dispatch is deliberate and never integrates worker output into the base checkout. See [docs/pro-escalations/USER_FLOW.md](docs/pro-escalations/USER_FLOW.md) and [docs/pro-escalations/ARCHITECTURE.md](docs/pro-escalations/ARCHITECTURE.md).

### Core Codex jobs

| Tool | Purpose | Read-only |
| --- | --- | --- |
| `codex_plan_job` | Start a Codex analysis job using the configured sandbox | no in the full-power profile |
| `codex_apply_job` | Start an isolated Codex apply job in a git worktree | no |
| `codex_get_status` | Inspect async job state | yes |
| `codex_get_result` | Fetch completed job output | yes |
| `codex_get_diff` | Inspect a changed file diff from a completed apply job | yes |
| `codex_cancel_job` | Cancel a pending or running local Codex job | no |
| `codex_review` | Run Codex review on owned changes | yes |
| `codex_interactive` | Start an async Codex exec session job | no |
| `codex_interactive_reply` | Continue a Codex session through an async job | no |
| `codex_resume` | Resume a prior Codex session through an async job | no |
| `codex_list_sessions` | List bounded PatchBay-known and configured Codex-home session metadata without transcripts or source paths | yes |

### Workspace context

| Tool | Purpose | Read-only |
| --- | --- | --- |
| `codex_self_test` | Check connector readiness and Server URL metadata | yes |
| `codex_open_workspace` | Orient ChatGPT to an allowed workspace | yes |
| `codex_list_workspaces` | List configured workspaces | yes |
| `codex_workspace_snapshot` | Return git status, recent commits, `.ai-bridge`, and compact tree | yes |
| `codex_inventory` | Return tool modes, skills, git state, and power-mode settings | yes |
| `codex_repo_tree` | Return a bounded repository tree | yes |
| `codex_read_file` | Read a bounded text file slice | yes |
| `codex_search_repo` | Search the repo with bounded, redacted results | yes |
| `codex_git_status` | Show branch and changed files without bash | yes |
| `codex_git_diff` | Show bounded git diff without bash | yes |
| `codex_show_changes` | Return review-oriented status and optional diff, optionally scoped to one file | yes |
| `codex_load_context` | Load AGENTS, selected files, git, and `.ai-bridge` context | yes |
| `codex_list_skills` | List discovered skills with sanitized paths | yes |
| `codex_load_skill` | Load a bounded discovered `SKILL.md` | yes |

### Handoff and context artifacts

| Tool | Purpose | Read-only |
| --- | --- | --- |
| `codex_export_context` | Write selected context under `.ai-bridge` | no |
| `codex_write_handoff` | Write `.ai-bridge/current-plan.md` | no |
| `codex_get_handoff_status` | Read `.ai-bridge` status artifacts | yes |
| `codex_get_handoff_diff` | Read bounded handoff diff artifacts | yes |

Local handoff commands are available without attaching ChatGPT:

```bash
python scripts/handoff.py execute --root /path/to/repo --agent custom --command-template "my-agent --task-file {{plan_file}}" --yes
python scripts/handoff.py watch --root /path/to/repo --agent custom --command-template "my-agent --task-file {{plan_file}}" --once --yes
python scripts/pro_context.py bundle --root /path/to/repo --path README.md --include-diff
python scripts/pro_context.py apply --root /path/to/repo --file plan.md --agent codex
```

### Optional power tools

These are public capabilities in `full` tool mode. The current runtime
permission profile enables their authority by default, but the recommended
ChatGPT-facing default is `worker`, which hides these power tools until the
surface is deliberately broadened. Disable them in `config.yaml` or at launch
when you want a narrower run:

| Tool | Required config |
| --- | --- |
| `codex_write_file` | `power_tools.direct_write: true` |
| `codex_edit_file` | `power_tools.direct_write: true` |
| `codex_run_command` | `power_tools.bash_mode: safe` or `full` |
| `codex_read_session` | `power_tools.codex_session_read: true` |

`tools/list` is runtime-aware for these capabilities: if a profile disables
direct write, bash, or session transcript reads, the corresponding canonical
tools and compatibility aliases are not advertised and calls to them are
rejected. The checked-in profile remains intentionally full-authority at the
runtime permission layer, while the default ChatGPT-facing catalog remains
worker-first. On a dedicated full-access workbench or VM, ChatGPT should treat
missing dependencies, repo-local virtual environments, verification commands,
commits, and authorized private-repo pushes as normal engineering work when the
user asked for an end-to-end result; it should ask first for public,
production, paid-resource, credential-changing, or irreversible external
actions.

## ChatGPT Metadata And Tool Card

`tools/list` includes the data metadata every public tool needs: `title`, read/write/open-world annotations, top-level `securitySchemes`, `_meta.securitySchemes`, output schemas, and `openai/fileParams` where a tool receives ChatGPT files. It does not advertise a widget by default.

PatchBay still contains a compact passive Apps card resource:

```text
ui://widget/patchbay-tool-card-v2.html
```

The card is disabled by default because repeated ChatGPT Apps iframes made long PatchBay sessions heavy and difficult to use on phones and tablets. This is a server/operator configuration choice, not a ChatGPT tool and not something the model can toggle. When `app.tool_cards: false`, `tools/list` omits `_meta.ui.resourceUri`, `openai/outputTemplate`, and invocation labels, and `resources/list` returns no PatchBay widget resource. Tool `structuredContent` remains unchanged for ChatGPT reasoning.

Operators can opt in by setting:

```yaml
app:
  tool_cards: true
```

When enabled, clients can fetch the card with `resources/list` and `resources/read`. The MIME type is `text/html;profile=mcp-app`. The card is a lightweight receipt: it shows a human tool label, a human status phrase, and one human-readable detail line while leaving the full tool payload in `structuredContent` for ChatGPT reasoning and later inspection. Internal tool identifiers may be used only as hidden component metadata or local widget inference; they are not the visible card language. It hydrates from both MCP Apps bridge tool-result notifications and ChatGPT `window.openai` compatibility globals, and it shows a compact widget-error state instead of staying on the initial waiting state if rendering fails. It remains passive: it does not initiate tool calls. The legacy `ui://widget/patchbay-tool-card-v1.html` URI remains readable only when tool cards are enabled.

## Power Boundary And Controls

PatchBay is deliberately powerful. These controls are not the product story; they are the boundary that makes it practical to aim ChatGPT at real local engineering work without hiding what is reading, writing, executing, or applying.

- Keep first runs on disposable repos.
- The checked-in profile is intentionally full-authority at the runtime layer:
  `/` allowed root, `danger-full-access`, direct writes, full bash, and Codex
  session reads are available when the visible tool mode exposes them.
- On a private full-access VM/workbench, dependency installation and repo-local
  environment setup are expected parts of verification. If a worker needs
  `pytest`, `pandas`, `openpyxl`, Node packages, or another project dependency,
  it should create/reuse the documented environment and install what is needed
  instead of reporting weaker verification, unless repo docs or external-risk
  boundaries say otherwise.
- The default ChatGPT-facing tool mode is `worker`, so the app starts from the
  manager surface instead of the full power-user catalog.
- For public or shared runs, narrow `repositories.allowed`, set `power_tools.bash_mode: "off"` or `"safe"`, and disable `allow_dangerously_bypass`.
- Keep CORS disabled unless a trusted local UI requires it.
- Do not expose public URLs without `PATCHBAY_HTTP_TOKEN`.
- Do not put secrets, credentials, customer data, or private logs in prompts or repos used for testing.
- With `blocked_globs: []`, workspace tools do not block secret-like paths by glob; symlink escapes, binary files, size caps, and output redaction still apply.
- `codex_get_diff` only returns diffs from completed apply jobs and files proven changed by git status/diff.
- Handoff writes are scoped to `.ai-bridge`.
- Direct writes, bash, and transcript reads are enabled in the checked-in
  runtime permission profile, but hidden from the default `worker` tool surface.
- Child Codex and bash processes inherit the full process environment when `allowed_env_keys: ["*"]`.
- Worker model/reasoning selection uses `codex debug models` or the local Codex model cache for bounded public metadata. It returns only model ids, concise option metadata, and advisory Spark / GPT-5.4 Mini / GPT-5.4 / GPT-5.5 selection guidance, not raw Codex config paths, prompts, provider credentials, or auth data.
- Codex auth/session startup is serialized by default per Codex home to avoid concurrent refresh-token races. If Codex itself reports `codex_auth_refresh_failed`, the operator must run `codex login` for the same host/user/CODEX_HOME before retrying workers.
- Audit logs and job state do not store raw prompt bodies by default.
- Job stdout/stderr artifacts are redacted and capped unless `logging.write_raw_job_logs: true`.

## Development And Verification

Run the local baseline:

```bash
codex --version
PYTHONDONTWRITEBYTECODE=1 python -m compileall -q src scripts tests
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests -q
PYTHONDONTWRITEBYTECODE=1 python scripts/live_mcp_eval.py --json
```

The live eval does not use ChatGPT and does not open a public tunnel. It starts the real launcher/server against a temporary repo and behaves like a compact MCP client. External-style coverage is tracked separately with a tokenized public-tunnel MCP simulator; the latest ngrok run passed the artifact inbox worker flow but still was not the real ChatGPT UI.

For shared-server coordination checks, run:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/real_mcp_worker_trial.py --multi-client --tool-mode worker --json
```

That direct MCP trial uses two logical MCP sessions against a disposable repo. It verifies session-local tool modes, shared inspection, cross-owner mutation refusal, explicit takeover, ownership transfer, preview-before-integrate, no automatic commit, and sanitized private evidence under `.local/validation/`.

## Documentation Map

- [docs/README.md](docs/README.md): full documentation index.
- [QUICKSTART.md](QUICKSTART.md): disposable first-run flow.
- [docs/user/chatgpt-instructions.md](docs/user/chatgpt-instructions.md): tool-use guidance for ChatGPT or another MCP client.
- [docs/architecture/overview.md](docs/architecture/overview.md): current hybrid architecture.
- [docs/reference/public-tool-surface.md](docs/reference/public-tool-surface.md): tool tiers, schemas, aliases, and metadata policy.
- [docs/worker-bridge/README.md](docs/worker-bridge/README.md): natural-language worker bridge architecture and implementation history.
- [docs/pro-escalations/ARCHITECTURE.md](docs/pro-escalations/ARCHITECTURE.md): reverse local-to-ChatGPT Pro request architecture.
- [docs/pro-escalations/USER_FLOW.md](docs/pro-escalations/USER_FLOW.md): operator and ChatGPT flow for Pro Requests.
- [docs/reference/context-and-handoff.md](docs/reference/context-and-handoff.md): AGENTS, skills, context packs, and `.ai-bridge`.
- [docs/project/why-patchbay.md](docs/project/why-patchbay.md): product purpose and value proposition.
- [SECURITY.md](SECURITY.md): vulnerability reporting and operator warnings.
- [docs/security/product-boundary.md](docs/security/product-boundary.md): power-control model.
- [TESTING.md](TESTING.md): local checks and live MCP evals.
- [docs/testing/evals.md](docs/testing/evals.md): release eval matrix.
- [NOTICE](NOTICE): CodexPro attribution.

## Credits

PatchBay includes behavior, documentation, tests, and implementation patterns derived from or inspired by open-source CodexPro work. See [NOTICE](NOTICE) for attribution and license details.

## License

MIT
