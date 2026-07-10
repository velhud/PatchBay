# Quick Start

This quick start is centered on connecting ChatGPT to PatchBay. Use a disposable repository first: PatchBay gives ChatGPT a powerful route into local Codex and local repositories, so verify the connector flow before using it on important work.

## 1. Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e ".[test]"
codex login
codex --version
```

The current verified baseline is `codex-cli 0.144.1`.

## 2. Create A Disposable Repo

```bash
tmpdir=$(mktemp -d)
mkdir -p "$tmpdir/repo"
cd "$tmpdir/repo"
git init
printf '# Disposable PatchBay Eval\n' > README.md
mkdir -p src
printf 'print("hello")\n' > src/app.py
git add README.md src/app.py
git -c user.name='Eval User' -c user.email='eval@example.invalid' commit -m init
```

## 3. Check Local Readiness

From PatchBay repo:

```bash
patchbay doctor
patchbay start --root "$tmpdir/repo" --tool-mode worker --print-only
patchbay start --root "$tmpdir/repo" --tool-mode worker --print-only --json
```

The output should show `name: patchbay`, `ready: true`, a local MCP URL, a
`ChatGPT setup` section, and no raw token unless you explicitly ask for one.
The JSON output includes `setup_guide` with the Server URL, authentication
setting, Developer Mode steps, useful profile/restart commands, and
token/tunnel notes.

## 4. Start PatchBay For ChatGPT

```bash
export PATCHBAY_HTTP_TOKEN='<long-random-token>'
patchbay start \
  --root "$tmpdir/repo" \
  --tunnel-mode cloudflare \
  --tool-mode worker \
  --save-profile \
  --reveal-token
```

Copy the full HTTPS Server URL printed by the launcher. It should end with `/mcp` and include `patchbay_token=...`.

The local endpoint still exists for local MCP clients:

```text
http://127.0.0.1:8000/mcp
```

For MCP hosts that prefer stdio, use:

```bash
patchbay stdio --config config.yaml
```

For local-only MCP clients, no token is required by default. If `PATCHBAY_HTTP_TOKEN` is set, the same endpoint requires Bearer or query-token auth. ChatGPT web normally needs a public HTTPS URL, so use a tunnel for the first real ChatGPT connector run.

The launcher supervises the local server and tunnel process together. It validates tunnel binaries before use. Install Cloudflare Tunnel explicitly with `patchbay install-cloudflared`, or install/configure `ngrok` yourself and use `patchbay ngrok --hostname <reserved-domain>`. Use `--tool-mode worker` first so ChatGPT sees the worker-first surface instead of the full power-user catalog.

OpenAI's Apps SDK docs describe the ChatGPT connector flow as: enable Developer Mode, create a connector, paste an HTTPS `/mcp` URL, then open a new chat and add the connector from the `+` / More menu. References:

- [OpenAI Apps SDK quickstart: Add your app to ChatGPT](https://developers.openai.com/apps-sdk/quickstart#add-your-app-to-chatgpt)
- [OpenAI Apps SDK: Connect from ChatGPT](https://developers.openai.com/apps-sdk/deploy/connect-chatgpt#create-a-connector)

For multi-repository testing, include every repository when starting PatchBay. `--root` is the default workspace and resets allowed roots to that workspace unless extra roots are supplied:

```bash
patchbay start \
  --root "$repo_a" \
  --allow-root "$repo_b" \
  --tunnel-mode cloudflare \
  --tool-mode worker \
  --reveal-token
```

If ChatGPT or another MCP client gets "Path is outside configured allowed roots," restart with the missing repository passed through `--allow-root` or add it to `repositories.allowed`.

## 5. Create The ChatGPT Connector

In ChatGPT, open:

```text
Settings
-> Apps & Connectors
-> Advanced settings
-> Developer mode: on
-> Enforce CSP in developer mode: on
-> Settings -> Connectors -> Create
```

Use these settings:

```text
Name: PatchBay
Description: Route ChatGPT context into local Codex workers
Connector URL / Server URL: paste the full HTTPS /mcp URL printed by patchbay start --reveal-token
Authentication: No Authentication / None
```

Choose `No Authentication / None` inside ChatGPT because the copied Server URL already carries PatchBay token as a query parameter. Do not configure OAuth or an API key in ChatGPT for this local bridge.

The optional PatchBay tool-card widget is off by default because repeated Apps cards made long ChatGPT sessions heavy on mobile and tablet browsers. Normal PatchBay use does not require cards; ChatGPT still receives structured tool results. If an operator intentionally enables `app.tool_cards: true`, keep `Enforce CSP in developer mode` enabled because the widget resource is designed for the CSP-enabled path.

After the connector is created, ChatGPT should show the tools PatchBay advertises. Open a new chat, click `+`, add PatchBay from the More menu, and send:

```text
Use PatchBay. Act as the manager of local Codex workers, not as the primary file reader. Call codex_self_test, then codex_open_workspace, then tell me what repo you can see, which worker tools are available, and how you would split a non-trivial task across workers.
```

Expected result:

- `codex_self_test` reports `name: patchbay`, readiness, the active tool mode, and shared-server coordination metadata.
- `codex_open_workspace` reports the disposable repo, branch, git status, AGENTS/context hints, and next suggested tools.
- In worker mode, ChatGPT should see `codex_worker_*` tools plus the context tools needed to brief workers.
- Shared worker and artifact views may show ownership statuses. `current_client` means the current scoped owner matches, `legacy_connection` means an older durable record lacks owner-scope metadata, and `other_token_owner` means the item came from a different tokenized Server URL. Mutating another owner still requires user-confirmed `takeover: true`.

During a run, `codex_tool_mode_info` can compare `worker`, `standard`, `full`, and `minimal`; `codex_tool_mode_switch` can request a session-local mode change. Direct MCP clients that re-run `tools/list` on the same MCP session see the new catalog, while other sessions keep their own mode. ChatGPT Developer Mode may still require refreshing or reconnecting the connector before newly exposed tools appear.

Never commit, screenshot, or share the full tokenized URL.

## 6. Run The Local MCP Eval

In another terminal:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/live_mcp_eval.py --json
```

This does not use ChatGPT and does not open a public tunnel. It starts a temporary server and probes MCP initialize, tool listing, resources, workspace context, worker-mode aliases absence, path guards, and read behavior. Run it with `--tool-mode full` when you deliberately want to verify compatibility aliases, direct write, and full bash on the disposable repo.

## 7. Try Light Workspace Orientation

Ask ChatGPT to use:

1. `codex_self_test`
2. `codex_open_workspace`
3. `codex_list_workspaces` when the repo name/path is unclear
4. `codex_repo_tree`
5. `codex_read_file`
6. `codex_search_repo`
7. `codex_show_changes` when the visible tool mode exposes it

This path is for brief orientation and verification. It should not become the normal development loop for non-trivial work. The normal PatchBay posture is ChatGPT as lead: ask local Codex workers natural-language questions, assign them work, read their reports, and inspect direct files/diffs only when needed to verify evidence. If ChatGPT starts making repeated direct read/search calls to understand the repo, it should stop and delegate that investigation to a worker. The checked-in runtime permission profile enables direct writes and full bash, but the default `worker` tool surface hides those power tools from ChatGPT until the surface is deliberately broadened. Using `--root "$tmpdir/repo"` keeps runtime authority scoped to the disposable repo for this first run.

## 8. Try A Pro Escalation Loop

Use this when local Codex has prepared a blocked-problem report for ChatGPT Pro.

From the PatchBay repo, create a small report:

```bash
cat > "$tmpdir/pro-request.md" <<'EOF'
# Pro Escalation

Need a concise plan for the disposable repo.
EOF

patchbay pro-request create \
  --repo "$tmpdir/repo" \
  --title "Disposable Pro request" \
  --report "$tmpdir/pro-request.md" \
  --json
```

In ChatGPT, call:

1. `codex_self_test`
2. `codex_pro_request_list`
3. `codex_pro_request_read`
4. `codex_pro_request_claim`
5. `codex_pro_request_respond`

`codex_pro_request_respond` stores the answer only. It does not message a worker, edit files, apply code, or commit. Use `codex_pro_request_dispatch` only when the user explicitly wants the stored answer sent to an idle origin worker or a new isolated worker.

## 9. Try ChatGPT With A Named Worker

Use this for durable isolated implementation:

For investigation, ask a worker a natural question instead of manually searching every file yourself:

```json
{
  "name": "Repository Investigator",
  "brief": "Inspect this repository, explain the main structure, identify where the reported problem probably lives, and recommend the next implementation task. Do not edit files.",
  "repo_path": "/absolute/path/to/disposable/repo",
  "workspace_mode": "read_only"
}
```

First, if the task needs a specific Codex model or reasoning depth, call `codex_worker_options`:

```json
{
  "model": "gpt-5.6-terra"
}
```

Then pass the selected values only when they matter. Optimize for expected subscription use to a verified result: GPT-5.6 Luna is the compact standard default, GPT-5.6 Terra is the main serious worker, and GPT-5.6 Sol is the highest-authority lane. For bounded small-worker assignments that either Spark or GPT-5.4 Mini can handle, choose Spark first because it is dramatically faster and uses a separate preview quota. If Spark is unavailable, depleted, or too context-constrained, immediately continue or retry the same assignment with GPT-5.4 Mini. GPT-5.4 and GPT-5.5 are availability or evidence-backed regression fallbacks. The installed Codex catalog returned by `codex_worker_options` is the authority for which models and reasoning efforts are currently usable.

```json
{
  "name": "Repository Implementer",
  "brief": "Create a tiny note file named worker-note.txt, run the smallest useful verification, and report what changed.",
  "repo_path": "/absolute/path/to/disposable/repo",
  "model": "gpt-5.6-terra",
  "reasoning_effort": "high"
}
```

`codex_worker_start` defaults to `workspace_mode: "isolated_write"`, so the worker writes in an external private worktree. Omit `model` and `reasoning_effort` to use Codex defaults. PatchBay accepts `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, and `ultra`, but each model may support only a subset; use the menu returned by `codex_worker_options`. `max` is the deepest single-agent effort. In Codex CLI `0.144.1`, `ultra` is available on supported models such as Terra and Sol and may automatically delegate inside one worker; prefer explicit named PatchBay workers when visible lanes, reports, worktrees, or integration control matter. Follow-up `codex_worker_message` calls keep the worker's prior model/reasoning unless you intentionally override them. Treat the worker as a continuing specialist: if the first report is too compressed, missing evidence, missing validation, contradicted by another worker, or important enough to drive a decision, message the same worker again instead of treating the first answer as final. For consequential writable audits or implementation, ask the worker to create a durable report file such as `worker-report-<topic>.md` or changed-file evidence in its workspace. Read-only workers do not write source-checkout report files; they expose structured reports, partial reports, and live checkpoints through PatchBay. Direct read/search tools remain available for orientation, focused verification, exact line checks, and tiny tasks, but broad work should be delegated. For advisory work, ask for read-only mode explicitly:

```json
{
  "name": "Repository Investigator",
  "brief": "Inspect the repository layout and report the main architecture boundary. Do not edit files.",
  "repo_path": "/absolute/path/to/disposable/repo",
  "workspace_mode": "read_only"
}
```

Call `codex_worker_start`, then inspect with:

```json
{"worker": "Repository Investigator", "wait_seconds": 10}
```

using `codex_worker_inspect`. For running teams, call `codex_worker_status` as the compact pull-based status bar: it returns active/quiet/stale/lost counts, deltas since the last check, one short line per worker, hidden-history count, and `recommended_next_poll_seconds`. Default `scope=current` shows the current work run plus live/problem workers instead of every old completed/stopped worker. Use `scope=conversation` when deliberately continuing workers from the same ChatGPT conversation, `scope=recent` for recently active work, and `scope=history` only when the durable archive is needed. Compact status omits raw shell command text; use `view: "status"` only for deliberate debugging when the command preview matters. For normal monitoring, wait about 10-20 seconds between status/list/wait/compact-inspect checks; do not poll every few seconds unless the user explicitly asks for near-real-time monitoring or the status says immediate recovery is needed. If monitoring is called too early, PatchBay returns `poll_too_early: true`, `status_current: false`, and `retry_after_seconds` without resetting activity deltas; this is not a failure and does not limit worker start/message/stop/integrate or focused file/diff/integration inspection. Use `codex_worker_wait` when the right management action is simply to wait once and receive fresh status. If events, output, or partial notes are changing, wait instead of cancelling; no final report yet does not mean no progress. For one worker, use `{"worker": "Repository Investigator", "view": "compact"}` or `view: "status"` and read `liveness`, `latest_partial_note`, `latest_checkpoints`, and `report_artifacts` before deciding a worker is stuck or stopping it. Use `{"worker": "Repository Implementer", "view": "changes"}` to list worker changes, `{"worker": "Repository Implementer", "view": "file", "file_path": "worker-note.txt"}` to read worker-side file content before integration, and `{"worker": "Repository Implementer", "view": "diff", "file_path": "worker-note.txt"}` to inspect one file's patch. File reads are paged: if the result includes `next_start_line`, call the same file tool again with that `start_line` instead of raising `max_bytes` into a very large single response. `codex_read_file` reads the base checkout, so it will not see worker-created files until after explicit integration; its `max_bytes` caps the returned page and does not need to exceed the whole file size for a small line range. This cap is a response-stability control, not a request to save tokens or skip evidence. After restarting PatchBay, `codex_worker_list(scope="conversation")` should still show same-conversation workers when ChatGPT supplies session metadata, and `codex_worker_message` should continue the same Codex conversation by name when the worker has a session. Use `codex_worker_list` filters such as `active_only`, `include_stopped: false`, `owned_only`, and `created_after` for additional narrowing. Use `context_from_workers` when a synthesis or review worker should compare prior worker reports instead of starting from scratch; choose `context_detail: "review"` for report plus changed-file inventory plus bounded diff. Use `auto_suffix: true` when rerunning a phase with the same worker name, `include_untracked_from_base` for selected accepted untracked phase artifacts, and `accepted_dirty_base` during preview/integration when known phase files are dirty but unrelated dirty files should still block.

For larger tasks, start several workers with separate responsibilities instead of asking ChatGPT to precompute every path. More workers are good when their briefs are clear and responsibilities do not overlap unnecessarily; use the configured worker capacity instead of imposing an artificial one-or-two-worker limit:

```json
{
  "name": "Backend Implementer",
  "brief": "Own the backend/API side of the requested feature. Find the relevant files yourself, implement in your isolated worktree, run focused checks, and report behavior, changed files, and any UI contract."
}
```

```json
{
  "name": "UI Implementer",
  "brief": "Own the UI side of the requested feature. Find the relevant UI structure yourself, implement in your isolated worktree, and report integration assumptions."
}
```

If ChatGPT creates a file or zip that should be used by a local worker, first import it:

```json
{
  "action": "import_file",
  "artifact_file": "<ChatGPT supplied file parameter>",
  "label": "update package"
}
```

using `codex_worker_inbox`. Then pass the returned id to an isolated worker:

```json
{
  "name": "Repository Implementer",
  "brief": "Use the imported update package as source material and adapt it to this repo.",
  "context_from_artifacts": ["art_example123"]
}
```

Artifact import is local context only: it does not edit the repo. Multiple files or zips can be imported in sequence, then attached to `codex_worker_start` or later `codex_worker_message` calls.

For local worker-first testing without a tunnel, start PatchBay with:

```bash
patchbay start --root "$tmpdir/repo" --tool-mode worker
```

This hides low-level job/session controls and compatibility aliases while keeping worker tools and the read-only context tools needed to brief them.

## 10. Try ChatGPT As Codex Controller

Call `codex_plan_job`:

```json
{
  "spec": "Summarize the repository layout and identify one useful test to add.",
  "repo_path": "/absolute/path/to/disposable/repo"
}
```

Then poll and fetch:

```json
{"job_id": "<returned-job-id>"}
```

with `codex_get_status` and `codex_get_result`.

For an implementation test, call `codex_apply_job` only on the disposable repo and inspect diffs with `codex_get_diff` before copying or merging anything.

## 11. Resume Flow

When `codex_get_result` returns `session_ref`, keep it. Continue later with:

- `codex_resume` for a resumed Codex job;
- `codex_interactive_reply` for a continuation job;
- `codex_list_sessions` to find known metadata-only session ids.

Transcript bodies are available when `power_tools.codex_session_read` is enabled and ChatGPT is deliberately switched to a tool mode that advertises `codex_read_session`, bounded and redacted. Disable `power_tools.codex_session_read` when you do not want ChatGPT to inspect local Codex transcripts.
