# Testing

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

## Run a Read-Only Job

Use a repository path that is allowed in `config.yaml`:

```bash
curl -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Mcp-Session-Id: <session-id>" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"query_text_analytics","arguments":{"spec":"List Python files","data_source":"/absolute/path/to/allowed/repo"}}}'
```

## Checklist

- Server starts on port 8000.
- `/` returns health metadata.
- `/mcp` accepts JSON-RPC requests.
- `tools/list` returns the tool catalog.
- A read-only job runs against an allowed git repository.
- Disallowed repository paths are rejected.
