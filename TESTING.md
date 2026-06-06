# Testing

## Unit And Smoke Checks

These checks do not require a Codex login:

```bash
python -m compileall .
python -m pytest tests -q
```

They verify:

- advertised public tool names;
- rejection of hidden/internal tools;
- read/write tool metadata;
- conservative security defaults;
- path validation;
- redaction helpers;
- MCP initialize instructions.

## Server Health

```bash
python server.py
curl http://127.0.0.1:8000/
```

Expected response includes:

```json
{
  "name": "codex-mcp-wrapper",
  "transport": "streamable-http",
  "status": "running"
}
```

## Initialize MCP Session

```bash
curl -i -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"curl","version":"1.0"}}}'
```

Save the returned `Mcp-Session-Id` header.

## List Tools

```bash
curl -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Mcp-Session-Id: <session-id>" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
```

## Run a Read-only Job

Use a repository path that is allowed in `config.yaml`:

```bash
curl -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Mcp-Session-Id: <session-id>" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"codex_plan_job","arguments":{"spec":"List Python files","repo_path":"/absolute/path/to/allowed/repo"}}}'
```

## Manual Smoke Test

With the server running:

```bash
python scripts/manual_mcp_smoke.py
```

## Checklist

- Server starts on port 8000.
- `/` returns health metadata.
- `/mcp` accepts JSON-RPC requests.
- `tools/list` returns the public Codex tool catalog.
- A read-only job runs against an allowed git repository.
- Disallowed repository paths are rejected.
