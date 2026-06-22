# Testing

The test strategy separates four things:

- static/unit checks that do not require Codex login;
- live local MCP probing without ChatGPT or a public tunnel;
- real Codex CLI execution through the wrapper;
- release evals that still need real ChatGPT Developer Mode and tunnel coverage.

## Baseline

```bash
codex --version
PYTHONDONTWRITEBYTECODE=1 python -m compileall -q .
PYTHONDONTWRITEBYTECODE=1 python -m pytest tests -q
```

Current verified Codex CLI baseline:

```text
codex-cli 0.141.0
```

The unit suite verifies:

- advertised public tool names and compatibility aliases;
- rejection of hidden/internal tools;
- read/write/open-world metadata;
- public schema validation and argument translation;
- connector doctor and auth policy behavior;
- MCP request body size limits;
- durable redacted job metadata persistence;
- current Codex JSONL `agent_message` result parsing;
- redacted/capped job stdout/stderr artifacts;
- strict completed-apply-job diff retrieval;
- `codex review` prompt stdin transport and config override allowlisting;
- metadata-only session listing;
- gated, bounded, redacted Codex session transcript reads;
- process cancellation for running jobs;
- optional direct workspace write/edit and command power tools;
- launcher profile storage and runtime config generation;
- fake public tunnel process supervision;
- ChatGPT Apps tool-card resource discovery;
- conservative security defaults;
- path validation and symlink escape rejection;
- redaction helpers;
- MCP initialize instructions.

## Connector Doctor

```bash
python scripts/doctor.py
python scripts/doctor.py --json
python scripts/start.py --root /absolute/path/to/allowed/repo --print-only
python scripts/start.py --root /absolute/path/to/allowed/repo --print-only --json
```

Expected output includes readiness checks, the local MCP URL, a redacted ChatGPT Server URL preview when token auth is enabled, and no raw token value.

For public ChatGPT tunnel previews, set `CODEX_MCP_HTTP_TOKEN` before using `--public-base-url`; the launcher should fail closed without that token.

## Live Local MCP Eval

Run a real launcher/server/probe cycle without ChatGPT and without a public tunnel:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/live_mcp_eval.py --json
```

The eval creates a temporary git repo with `AGENTS.md`, source files, `.env`, a symlink escape, and a repo-local `SKILL.md`; starts `scripts/start.py`; then probes:

- MCP health and initialize;
- `tools/list`;
- Apps resources;
- workspace open;
- skill list/load;
- file read and alias read;
- git status;
- workspace snapshot;
- show changes alias;
- blocked `.env` read;
- blocked symlink read;
- disabled direct write;
- `codex_self_test`.

This test proves the local MCP surface behaves like a compact ChatGPT-style client, but it does not prove ChatGPT Developer Mode itself.

## Real Codex CLI Through MCP

For execution changes, run a disposable real-Codex plan job through MCP. The expected path is:

1. start `scripts/start.py` against a disposable git repo;
2. initialize MCP;
3. call `codex_plan_job`;
4. poll `codex_get_status`;
5. call `codex_get_result`;
6. confirm a clean structured summary and `session_ref` when Codex returns one.

The latest manual verification used Codex CLI `0.141.0` and confirmed the wrapper parses the current JSONL `item.completed` / `agent_message` result shape.

## Manual Curl Smoke

Start the server:

```bash
python scripts/start.py --root /absolute/path/to/allowed/repo
```

Health:

```bash
curl http://127.0.0.1:8000/
```

Initialize:

```bash
curl -i -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"curl","version":"1.0"}}}'
```

Save the returned `Mcp-Session-Id` header and use it for `tools/list`, `resources/list`, and `tools/call`.

## Release Evals Still Required

Before public release, run all of these against disposable repos:

- real ChatGPT Developer Mode connection;
- token-gated public tunnel connection;
- direct workspace orientation from ChatGPT;
- real `codex_plan_job` from ChatGPT;
- real `codex_apply_job` from ChatGPT with diff inspection;
- real resume or interactive continuation from ChatGPT using `session_ref`;
- `.ai-bridge` handoff write, local dry-run, local execute, and status/diff readback;
- blocked path, blocked symlink, disabled power-tool, unsafe bash, and missing-token failures.

## Checklist

- `codex --version` is recorded.
- Compile and pytest pass.
- `scripts/live_mcp_eval.py --json` passes.
- `tools/list` returns the expected public catalog and metadata.
- `resources/list` and `resources/read` return `ui://widget/codex-mcp-wrapper-tool-card-v1.html`.
- Async starter tools return `job_id`.
- Real Codex plan jobs complete through MCP.
- Structured Codex result parsing is clean.
- Direct write, bash, and transcript reads deny by default.
- Token-gated tunnel startup fails closed without `CODEX_MCP_HTTP_TOKEN`.
- Logs and runtime files do not contain real tokens, prompt bodies, or private paths in committed docs.
