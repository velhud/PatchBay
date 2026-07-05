# ChatGPT Connector Setup

This page holds the detailed ChatGPT connector notes that should not occupy the root README.

Use [QUICKSTART.md](../../QUICKSTART.md) for the full disposable-repository walkthrough. This page is the connector-specific reference.

## Start PatchBay for ChatGPT web

ChatGPT web normally needs an HTTPS `/mcp` URL. Start PatchBay with a tunnel and the worker-first tool surface:

```bash
export PATCHBAY_HTTP_TOKEN='<long-random-token>'
patchbay start \
  --root /path/to/repo \
  --tunnel-mode cloudflare \
  --tool-mode worker \
  --save-profile \
  --reveal-token
```

Copy the full tokenized Server URL printed by `--reveal-token`. It should look like:

```text
https://.../mcp?patchbay_token=...
```

Tokenized ChatGPT Server URLs are redacted unless `--reveal-token` is used. Do not commit, screenshot, or share the full tokenized URL.

To install Cloudflare Tunnel into PatchBay's local bin directory, run:

```bash
patchbay install-cloudflared
```

PatchBay also exposes tunnel shortcuts:

```bash
patchbay ngrok --root /path/to/repo --hostname your-domain.ngrok-free.dev --tool-mode worker --reveal-token
patchbay stable --root /path/to/repo --hostname patchbay.example.com --tunnel-name patchbay --tool-mode worker --reveal-token
```

## Create the ChatGPT connector

In ChatGPT:

```text
Settings -> Apps & Connectors -> Advanced settings
Developer mode: on
Enforce CSP in developer mode: on
Settings -> Connectors -> Create

Name: PatchBay
Description: Coordinate local Codex CLI workers from ChatGPT
Connector URL / Server URL: paste the full HTTPS /mcp URL printed by patchbay start --reveal-token
Authentication: No Authentication / None
```

The ChatGPT app auth setting is `No Authentication / None` because the Server URL already includes the private PatchBay token. Do not configure OAuth or paste an API key into ChatGPT for this local bridge.

After ChatGPT shows the advertised tools, open a new chat, add PatchBay from the `+` / More menu, and start with:

```text
Use PatchBay. Act as the manager of local Codex workers, not as the primary file reader. Call codex_self_test, then codex_open_workspace, then tell me what repo you can see, which worker tools are available, and how you would split a non-trivial task across workers.
```

Expected result:

- `codex_self_test` reports `name: patchbay`, readiness, active tool mode, and shared-server coordination metadata.
- `codex_open_workspace` reports the selected repository, branch, git status, AGENTS/context hints, and next suggested tools.
- In `worker` mode, ChatGPT should see `codex_worker_*` tools plus the read-only context tools needed to brief workers.

## Local MCP clients

For local MCP clients, start the local MCP server:

```bash
patchbay start --root /path/to/repo --tool-mode worker --save-profile
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

## Multiple repositories

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

## Tool modes from ChatGPT

ChatGPT can inspect mode choices with `codex_tool_mode_info` and request a session-local mode change with `codex_tool_mode_switch`. The switch does not rewrite config files.

Direct MCP clients that call `tools/list` again on the same MCP session will see the new catalog. Other sessions keep their own effective mode. ChatGPT Developer Mode may require refreshing the connector metadata before newly exposed tools appear.
