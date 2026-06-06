#!/usr/bin/env python3
"""
Test script for MCP protocol endpoints (Streamable HTTP transport)
"""
import json
import requests

BASE_URL = "http://localhost:8000"

# All 15 tools that should be present
ALL_TOOLS = [
    "codex_plan_job", "codex_apply_job", "codex_get_status", "codex_get_result", "codex_get_diff",
    "codex_interactive", "codex_interactive_reply", "codex_resume",
    "codex_review",
    "codex_cloud_exec", "codex_cloud_status", "codex_cloud_diff", "codex_apply_diff",
    "codex_get_config", "codex_sandbox",
]


def test_health():
    """Test health endpoint"""
    response = requests.get(f"{BASE_URL}/")
    print("=== Health Check ===")
    print(json.dumps(response.json(), indent=2))
    assert response.json()["transport"] == "streamable-http"
    return response.json()


def test_initialize():
    """Test MCP initialize"""
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0.0"}
        }
    }
    
    response = requests.post(f"{BASE_URL}/mcp", json=message)
    print("\n=== Initialize ===")
    print(f"Mcp-Session-Id: {response.headers.get('Mcp-Session-Id')}")
    print(json.dumps(response.json(), indent=2))
    
    return response.headers.get("Mcp-Session-Id"), response.json()


def test_tools_list(session_id: str):
    """Test tools/list"""
    message = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {}
    }
    
    response = requests.post(
        f"{BASE_URL}/mcp",
        json=message,
        headers={"Mcp-Session-Id": session_id}
    )
    print("\n=== Tools List ===")
    print(json.dumps(response.json(), indent=2))
    return response.json()


if __name__ == "__main__":
    print("Testing Codex MCP Server (Streamable HTTP)\n")
    
    # Test health
    test_health()
    
    # Test initialize
    session_id, init_result = test_initialize()
    
    if not session_id:
        print("\n✗ No session ID returned!")
        exit(1)
    
    # Test tools/list
    tools = test_tools_list(session_id)
    
    # Verify tools
    if "result" in tools and "tools" in tools["result"]:
        tool_names = [t["name"] for t in tools["result"]["tools"]]
        print(f"\n✓ Found {len(tool_names)} tools")
        
        missing = set(ALL_TOOLS) - set(tool_names)
        if missing:
            print(f"✗ Missing: {missing}")
        else:
            print("✓ All 15 tools present")
    else:
        print("\n✗ Invalid response format")
