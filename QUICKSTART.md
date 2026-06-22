# Quick Start

This quick start is for a disposable first run. Do not start by pointing ChatGPT at an important private repository.

## 1. Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
codex login
codex --version
```

The current verified baseline is `codex-cli 0.141.0`.

## 2. Create A Disposable Repo

```bash
tmpdir=$(mktemp -d)
mkdir -p "$tmpdir/repo"
cd "$tmpdir/repo"
git init
printf '# Disposable Codex MCP Eval\n' > README.md
mkdir -p src
printf 'print("hello")\n' > src/app.py
git add README.md src/app.py
git -c user.name='Eval User' -c user.email='eval@example.invalid' commit -m init
```

## 3. Check Connector Readiness

From the wrapper repo:

```bash
python scripts/doctor.py
python scripts/start.py --root "$tmpdir/repo" --print-only
```

The output should show a local MCP URL and no raw token unless you explicitly ask for one.

## 4. Start The Local MCP Server

```bash
python scripts/start.py --root "$tmpdir/repo" --save-profile
```

Local endpoint:

```text
http://127.0.0.1:8000/mcp
```

For local-only MCP clients, no token is required by default. If `CODEX_MCP_HTTP_TOKEN` is set, the same endpoint requires Bearer or query-token auth.

## 5. Run The Local MCP Eval

In another terminal:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/live_mcp_eval.py --json
```

This does not use ChatGPT and does not open a public tunnel. It starts a temporary server and probes MCP initialize, tool listing, resources, workspace context, aliases, path guards, and default power-tool denial.

## 6. First ChatGPT Connection Prep

ChatGPT web usually needs a public HTTPS URL to reach a local MCP server. Use a token before any tunnel:

```bash
export CODEX_MCP_HTTP_TOKEN='<long-random-token>'
python scripts/start.py \
  --root "$tmpdir/repo" \
  --tunnel-mode cloudflare \
  --save-profile
```

The launcher supervises the local server and tunnel process together. It does not install `cloudflared` or `ngrok`; install the provider CLI yourself.

For a stable provider hostname:

```bash
python scripts/start.py \
  --root "$tmpdir/repo" \
  --tunnel-mode ngrok \
  --hostname your-domain.ngrok-free.app
```

Use Bearer auth where the MCP client supports headers. For copied ChatGPT Server URLs, reveal the tokenized URL only when you are ready to paste it into ChatGPT:

```bash
python scripts/start.py --root "$tmpdir/repo" --tunnel-mode cloudflare --reveal-token
```

Never commit, screenshot, or share the full tokenized URL.

## 7. Try ChatGPT As Workspace Coder

Ask the MCP client to use:

1. `codex_self_test`
2. `codex_open_workspace`
3. `codex_workspace_snapshot`
4. `codex_read_file` or alias `read`
5. `codex_search_repo`
6. `codex_show_changes`

This path lets ChatGPT inspect and reason about the repo directly. Source writes remain disabled unless `power_tools.direct_write` is explicitly enabled.

## 8. Try ChatGPT As Codex Controller

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

## 9. Resume Flow

When `codex_get_result` returns `session_ref`, keep it. Continue later with:

- `codex_resume` for a resumed Codex job;
- `codex_interactive_reply` for a continuation job;
- `codex_list_sessions` to find known metadata-only session ids.

Transcript bodies remain unavailable unless `power_tools.codex_session_read` is explicitly enabled.
