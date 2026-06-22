from fastapi.testclient import TestClient

import server


def test_mcp_rejects_oversized_request_body():
    original_limit = server.config["server"].get("max_request_bytes")
    server.config["server"]["max_request_bytes"] = 32
    try:
        response = TestClient(server.app).post(
            "/mcp",
            content=b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"padding":"too large"}}',
            headers={"Content-Type": "application/json"},
        )
    finally:
        if original_limit is None:
            server.config["server"].pop("max_request_bytes", None)
        else:
            server.config["server"]["max_request_bytes"] = original_limit

    assert response.status_code == 413
    assert response.json()["error"]["message"] == "Request body too large"


def test_mcp_rejects_unknown_session_id():
    response = TestClient(server.app).post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Mcp-Session-Id": "deleted-session"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Unknown or expired MCP session"
