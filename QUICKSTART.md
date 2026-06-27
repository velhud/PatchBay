# Quick Start

This quick start is centered on connecting ChatGPT to PatchBay. Use a disposable repository first: PatchBay gives ChatGPT a powerful route into local Codex and local repositories, so verify the connector flow before using it on important work.

## 1. Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
codex login
codex --version
```

The current verified baseline is `codex-cli 0.142.2`.

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
python scripts/doctor.py
python scripts/start.py --root "$tmpdir/repo" --print-only
```

The output should show `name: patchbay`, `ready: true`, a local MCP URL, and no raw token unless you explicitly ask for one.

## 4. Start PatchBay For ChatGPT

```bash
export PATCHBAY_HTTP_TOKEN='<long-random-token>'
python scripts/start.py \
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

For local-only MCP clients, no token is required by default. If `PATCHBAY_HTTP_TOKEN` is set, the same endpoint requires Bearer or query-token auth. ChatGPT web normally needs a public HTTPS URL, so use a tunnel for the first real ChatGPT connector run.

The launcher supervises the local server and tunnel process together. It does not install `cloudflared` or `ngrok`; install the provider CLI yourself. Use `--tool-mode worker` first so ChatGPT sees the worker-first surface instead of the full power-user catalog.

OpenAI's Apps SDK docs describe the ChatGPT connector flow as: enable Developer Mode, create a connector, paste an HTTPS `/mcp` URL, then open a new chat and add the connector from the `+` / More menu. References:

- [OpenAI Apps SDK quickstart: Add your app to ChatGPT](https://developers.openai.com/apps-sdk/quickstart#add-your-app-to-chatgpt)
- [OpenAI Apps SDK: Connect from ChatGPT](https://developers.openai.com/apps-sdk/deploy/connect-chatgpt#create-a-connector)

For multi-repository testing, include every repository when starting PatchBay. `--root` is the default workspace and resets allowed roots to that workspace unless extra roots are supplied:

```bash
python scripts/start.py \
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
Connector URL / Server URL: paste the full HTTPS /mcp URL printed by scripts/start.py --reveal-token
Authentication: No Authentication / None
```

Choose `No Authentication / None` inside ChatGPT because the copied Server URL already carries PatchBay token as a query parameter. Do not configure OAuth or an API key in ChatGPT for this local bridge.

Keep `Enforce CSP in developer mode` enabled. The tool card resource is designed for the CSP-enabled path.

After the connector is created, ChatGPT should show the tools PatchBay advertises. Open a new chat, click `+`, add PatchBay from the More menu, and send:

```text
Use PatchBay. Call codex_self_test, then codex_open_workspace, then tell me what repo you can see and which worker tools are available.
```

Expected result:

- `codex_self_test` reports `name: patchbay`, readiness, the active tool mode, and shared-server coordination metadata.
- `codex_open_workspace` reports the disposable repo, branch, git status, AGENTS/context hints, and next suggested tools.
- In worker mode, ChatGPT should see `codex_worker_*` tools plus the context tools needed to brief workers.

During a run, `codex_tool_mode_info` can compare `worker`, `standard`, `full`, and `minimal`; `codex_tool_mode_switch` can request a session-local mode change. Direct MCP clients that re-run `tools/list` on the same MCP session see the new catalog, while other sessions keep their own mode. ChatGPT Developer Mode may still require refreshing or reconnecting the connector before newly exposed tools appear.

Never commit, screenshot, or share the full tokenized URL.

## 6. Run The Local MCP Eval

In another terminal:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/live_mcp_eval.py --json
```

This does not use ChatGPT and does not open a public tunnel. It starts a temporary server and probes MCP initialize, tool listing, resources, workspace context, aliases, path guards, and default power-tool denial.
In the current full-power profile it also verifies direct write and full bash on the disposable repo.

## 7. Try ChatGPT As Workspace Coder

Ask ChatGPT to use:

1. `codex_self_test`
2. `codex_open_workspace`
3. `codex_workspace_snapshot`
4. `codex_read_file`
5. `codex_search_repo`
6. `codex_show_changes`

This path lets ChatGPT inspect and reason about the repo directly. The checked-in profile enables direct writes and full bash; using `--root "$tmpdir/repo"` keeps that power scoped to the disposable repo for this first run.

## 8. Try ChatGPT With A Named Worker

Use this for durable isolated implementation:

First, if the task needs a specific Codex model or reasoning depth, call `codex_worker_options`:

```json
{
  "model": "gpt-5.5"
}
```

Then pass the selected values only when they matter:

```json
{
  "name": "Repository Implementer",
  "brief": "Create a tiny note file named worker-note.txt, run the smallest useful verification, and report what changed.",
  "repo_path": "/absolute/path/to/disposable/repo",
  "model": "gpt-5.5",
  "reasoning_effort": "high"
}
```

`codex_worker_start` defaults to `workspace_mode: "isolated_write"`, so the worker writes in an external private worktree. Omit `model` and `reasoning_effort` to use Codex defaults. Follow-up `codex_worker_message` calls keep the worker's prior model/reasoning unless you intentionally override them. For advisory work, ask for read-only mode explicitly:

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

using `codex_worker_inspect`. Use `{"worker": "Repository Implementer", "view": "changes"}` to list worker changes, `{"worker": "Repository Implementer", "view": "file", "file_path": "worker-note.txt"}` to read worker-side file content before integration, and `{"worker": "Repository Implementer", "view": "diff", "file_path": "worker-note.txt"}` to inspect one file's patch. `codex_read_file` reads the base checkout, so it will not see worker-created files until after explicit integration. After restarting PatchBay, `codex_worker_list` should still show same-workspace workers, and `codex_worker_message` should continue the same Codex conversation by name when the worker has a session.

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
python scripts/start.py --root "$tmpdir/repo" --tool-mode worker
```

This hides low-level job/session controls and compatibility aliases while keeping worker tools and the read-only context tools needed to brief them.

## 9. Try ChatGPT As Codex Controller

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

## 10. Resume Flow

When `codex_get_result` returns `session_ref`, keep it. Continue later with:

- `codex_resume` for a resumed Codex job;
- `codex_interactive_reply` for a continuation job;
- `codex_list_sessions` to find known metadata-only session ids.

Transcript bodies are available in the current full-power profile through `codex_read_session`, bounded and redacted. Disable `power_tools.codex_session_read` when you do not want ChatGPT to inspect local Codex transcripts.
