# Quick Start

## 1. Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
codex login
```

## 2. Configure Allowed Repositories

Edit `config.yaml`:

```yaml
repositories:
  default: /absolute/path/to/your/repo
  allowed:
    - /absolute/path/to/your/repo
```

Use a parent folder in `allowed` only if every child repository under that folder should be accessible to the MCP client.

## 3. Start

```bash
python server.py
```

The server runs locally at:

```text
http://127.0.0.1:8000/mcp
```

## 4. Connect an MCP Client

Use Streamable HTTP transport and the `/mcp` endpoint.

Do not expose this server with a public tunnel unless you add authentication and understand that connected clients can ask Codex to inspect or edit allowed repositories.

## 5. Try a Tool

Ask your MCP client to call `query_text_analytics` with:

```json
{
  "spec": "List the Python files and summarize the project layout.",
  "data_source": "/absolute/path/to/your/repo"
}
```

Then call `fetch_operation_result` with the returned `reference_id`.
