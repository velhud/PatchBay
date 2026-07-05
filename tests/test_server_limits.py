from fastapi.testclient import TestClient

from patchbay import server


def _mcp_post(client, message, session_id=None):
    headers = {}
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return client.post("/mcp", json=message, headers=headers)


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


def test_mcp_http_sessions_keep_tool_modes_separate():
    original_mode = server.config.setdefault("app", {}).get("tool_mode")
    original_sessions = dict(server.sessions)
    server.sessions.clear()
    server.config["app"]["tool_mode"] = "worker"
    client = TestClient(server.app)
    try:
        init_a = _mcp_post(
            client,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "http-session-a", "version": "test"},
                },
            },
        )
        init_b = _mcp_post(
            client,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "http-session-b", "version": "test"},
                },
            },
        )
        session_a = init_a.headers["Mcp-Session-Id"]
        session_b = init_b.headers["Mcp-Session-Id"]
        assert session_a != session_b

        before_a = _mcp_post(client, {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}, session_a)
        before_b = _mcp_post(client, {"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}}, session_b)
        assert "codex_resume" not in {tool["name"] for tool in before_a.json()["result"]["tools"]}
        assert "codex_resume" not in {tool["name"] for tool in before_b.json()["result"]["tools"]}

        switched_a = _mcp_post(
            client,
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "codex_tool_mode_switch",
                    "arguments": {"mode": "full", "reason": "Need low-level status tools in this chat."},
                },
            },
            session_a,
        )
        assert switched_a.json()["result"]["structuredContent"]["switch_scope"] == "session"

        after_a = _mcp_post(client, {"jsonrpc": "2.0", "id": 6, "method": "tools/list", "params": {}}, session_a)
        after_b = _mcp_post(client, {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}}, session_b)
        assert "codex_resume" in {tool["name"] for tool in after_a.json()["result"]["tools"]}
        assert "codex_resume" not in {tool["name"] for tool in after_b.json()["result"]["tools"]}

        self_test_b = _mcp_post(
            client,
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {"name": "codex_self_test", "arguments": {}},
            },
            session_b,
        )
        coordination = self_test_b.json()["result"]["structuredContent"]["coordination"]
        assert coordination["active_mcp_sessions"] == 2
        assert coordination["raw_session_ids_returned"] is False
        assert session_a not in str(coordination)
        assert session_b not in str(coordination)
    finally:
        if original_mode is None:
            server.config["app"].pop("tool_mode", None)
        else:
            server.config["app"]["tool_mode"] = original_mode
        server.sessions.clear()
        server.sessions.update(original_sessions)


def test_mcp_hashes_chatgpt_session_metadata_across_short_transports():
    original_sessions = dict(server.sessions)
    original_work_runs = dict(server.work_runs)
    server.sessions.clear()
    server.work_runs.clear()
    client = TestClient(server.app)
    try:
        message = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "codex_self_test",
                "arguments": {},
                "_meta": {
                    "openai/session": "chatgpt-conversation-raw",
                    "openai/subject": "chatgpt-user-raw",
                },
            },
        }
        first = _mcp_post(client, message)
        second = _mcp_post(client, {**message, "id": 2})

        first_coordination = first.json()["result"]["structuredContent"]["coordination"]
        second_coordination = second.json()["result"]["structuredContent"]["coordination"]
        first_client = first_coordination["client"]
        second_client = second_coordination["client"]

        assert first_client["chatgpt_session_ref"].startswith("chatgpt_session_")
        assert first_client["chatgpt_subject_ref"].startswith("chatgpt_subject_")
        assert first_client["chatgpt_session_ref"] == second_client["chatgpt_session_ref"]
        assert first_client["work_run_ref"] == second_client["work_run_ref"]
        assert first_client["work_run_ref"].startswith("run_")
        assert "chatgpt-conversation-raw" not in str(first_coordination)
        assert "chatgpt-user-raw" not in str(first_coordination)
    finally:
        server.sessions.clear()
        server.sessions.update(original_sessions)
        server.work_runs.clear()
        server.work_runs.update(original_work_runs)
