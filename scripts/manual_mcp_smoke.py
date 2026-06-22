#!/usr/bin/env python3
"""Manual smoke test for a running local MCP server."""
import json
import requests

BASE_URL = "http://localhost:8000"

PUBLIC_TOOLS = [
    "codex_open_workspace",
    "codex_repo_tree",
    "codex_read_file",
    "codex_search_repo",
    "codex_load_context",
    "codex_export_context",
    "codex_list_skills",
    "codex_load_skill",
    "codex_write_handoff",
    "codex_get_handoff_status",
    "codex_get_handoff_diff",
    "codex_write_file",
    "codex_edit_file",
    "codex_run_command",
    "codex_plan_job",
    "codex_apply_job",
    "codex_get_status",
    "codex_get_result",
    "codex_get_diff",
    "codex_cancel_job",
    "codex_review",
    "codex_list_sessions",
    "codex_read_session",
    "codex_resume",
    "codex_interactive",
    "codex_interactive_reply",
    "codex_self_test",
    "codex_get_config",
]

TOOL_CARD_URI = "ui://widget/codex-mcp-wrapper-tool-card-v1.html"


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
    for tool in payload["result"]["tools"]:
        meta = tool.get("_meta", {})
        assert meta.get("ui", {}).get("resourceUri") == TOOL_CARD_URI
        assert meta.get("openai/outputTemplate") == TOOL_CARD_URI


def list_resources(session_id: str):
    message = {"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}}
    response = requests.post(
        f"{BASE_URL}/mcp",
        json=message,
        headers={"Mcp-Session-Id": session_id},
        timeout=10,
    )
    print("\n=== Resources List ===")
    print(json.dumps(response.json(), indent=2))
    payload = response.json()
    uris = [resource["uri"] for resource in payload["result"]["resources"]]
    assert TOOL_CARD_URI in uris


def read_resource(session_id: str):
    message = {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "resources/read",
        "params": {"uri": TOOL_CARD_URI},
    }
    response = requests.post(
        f"{BASE_URL}/mcp",
        json=message,
        headers={"Mcp-Session-Id": session_id},
        timeout=10,
    )
    print("\n=== Resources Read ===")
    print(json.dumps(response.json(), indent=2))
    payload = response.json()
    content = payload["result"]["contents"][0]
    assert content["uri"] == TOOL_CARD_URI
    assert content["mimeType"] == "text/html;profile=mcp-app"
    assert "ui/notifications/tool-result" in content["text"]


if __name__ == "__main__":
    print("Testing running codex-mcp-wrapper server\n")
    health_check()
    session_id = initialize()
    if not session_id:
        raise SystemExit("No session ID returned")
    list_tools(session_id)
    list_resources(session_id)
    read_resource(session_id)
    print("\nManual smoke test passed")
