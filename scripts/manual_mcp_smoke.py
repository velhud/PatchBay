#!/usr/bin/env python3
"""Manual smoke test for a running local MCP server."""
import json
import requests

BASE_URL = "http://localhost:8000"

PUBLIC_TOOLS = [
    "codex_plan_job",
    "codex_apply_job",
    "codex_get_status",
    "codex_get_result",
    "codex_get_diff",
    "codex_review",
    "codex_resume",
    "codex_interactive",
    "codex_interactive_reply",
    "codex_get_config",
]


def health_check():
    response = requests.get(f"{BASE_URL}/", timeout=10)
    print("=== Health Check ===")
    print(json.dumps(response.json(), indent=2))
    assert response.json()["transport"] == "streamable-http"


def initialize():
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "manual-smoke-client", "version": "1.0.0"},
        },
    }
    response = requests.post(f"{BASE_URL}/mcp", json=message, timeout=10)
    print("\n=== Initialize ===")
    print(f"Mcp-Session-Id: {response.headers.get('Mcp-Session-Id')}")
    print(json.dumps(response.json(), indent=2))
    return response.headers.get("Mcp-Session-Id")


def list_tools(session_id: str):
    message = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    response = requests.post(
        f"{BASE_URL}/mcp",
        json=message,
        headers={"Mcp-Session-Id": session_id},
        timeout=10,
    )
    print("\n=== Tools List ===")
    print(json.dumps(response.json(), indent=2))
    payload = response.json()
    tool_names = [tool["name"] for tool in payload["result"]["tools"]]
    missing = set(PUBLIC_TOOLS) - set(tool_names)
    assert not missing, f"Missing public tools: {missing}"


if __name__ == "__main__":
    print("Testing running codex-mcp-wrapper server\n")
    health_check()
    session_id = initialize()
    if not session_id:
        raise SystemExit("No session ID returned")
    list_tools(session_id)
    print("\nManual smoke test passed")
