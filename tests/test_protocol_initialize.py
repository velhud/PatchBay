import asyncio

from mcp_protocol import MCPProtocol
from tool_resources import TOOL_CARD_MIME_TYPE, TOOL_CARD_URI


class DummyToolHandler:
    def __init__(self):
        self.calls = []

    async def handle_tool_call(self, tool_name, arguments):
        self.calls.append((tool_name, arguments))
        return {"tool_name": tool_name, "arguments": arguments}


def test_initialize_includes_server_instructions():
    protocol = MCPProtocol({}, DummyToolHandler())
    result = asyncio.run(protocol._handle_initialize({"protocolVersion": "2025-11-25"}))

    assert result["serverInfo"]["name"] == "codex-mcp-wrapper"
    assert result["serverInfo"]["version"] == "0.1.0"
    assert "instructions" in result
    assert "codex_self_test" in result["instructions"]
    assert "codex_open_workspace" in result["instructions"]
    assert "Workers are stateful by name" in result["instructions"]
    assert "integration_preview" in result["instructions"]
    assert "does not commit" in result["instructions"]
    assert "allowed roots" in result["instructions"]
    assert result["capabilities"]["resources"]["listChanged"] is False


def test_public_tool_call_validates_schema_before_handler():
    handler = DummyToolHandler()
    protocol = MCPProtocol({}, handler)

    response = asyncio.run(
        protocol.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "codex_plan_job",
                    "arguments": {"unexpected": True},
                },
            }
        )
    )

    assert response["error"]["code"] == -32602
    assert "Missing required argument 'spec'" in response["error"]["message"]
    assert handler.calls == []


def test_public_tool_call_translates_schema_valid_arguments():
    handler = DummyToolHandler()
    protocol = MCPProtocol({}, handler)

    result = asyncio.run(
        protocol._handle_tools_call(
            {
                "name": "codex_get_status",
                "arguments": {"job_id": "job-123"},
            }
        )
    )

    assert handler.calls == [("codex_get_status", {"job_id": "job-123"})]
    assert result["structuredContent"] == {
        "tool_name": "codex_get_status",
        "arguments": {"job_id": "job-123"},
    }
    assert "job-123" in result["content"][0]["text"]


def test_tool_mode_info_is_protocol_handled_and_lists_modes():
    handler = DummyToolHandler()
    protocol = MCPProtocol({"app": {"tool_mode": "worker"}}, handler)

    result = asyncio.run(
        protocol._handle_tools_call(
            {
                "name": "codex_tool_mode_info",
                "arguments": {},
            }
        )
    )

    payload = result["structuredContent"]
    assert payload["current_mode"] == "worker"
    assert payload["recommended_default"] == "worker"
    assert {"worker", "standard", "full", "minimal"} <= set(payload["available_modes"])
    modes = {mode["mode"]: mode for mode in payload["modes"]}
    assert "codex_worker_start" in modes["worker"]["tool_names"]
    assert "codex_resume" not in modes["worker"]["tool_names"]
    assert "codex_resume" in modes["full"]["tool_names"]
    assert "Refresh" in payload["chatgpt_refresh_note"]
    assert handler.calls == []


def test_tool_mode_switch_changes_process_local_catalog():
    handler = DummyToolHandler()
    protocol = MCPProtocol({"app": {"tool_mode": "worker"}}, handler)

    before = asyncio.run(protocol._handle_tools_list({}))
    before_names = {tool["name"] for tool in before["tools"]}
    assert "codex_resume" not in before_names
    assert "codex_tool_mode_switch" in before_names

    result = asyncio.run(
        protocol._handle_tools_call(
            {
                "name": "codex_tool_mode_switch",
                "arguments": {"mode": "full", "reason": "Need raw session continuation."},
            }
        )
    )

    payload = result["structuredContent"]
    assert payload["previous_mode"] == "worker"
    assert payload["current_mode"] == "full"
    assert payload["changed"] is True
    assert payload["persisted_to_config"] is False
    assert "config files were not modified" in payload["note"]
    assert "Refresh" in payload["chatgpt_refresh_note"]
    assert handler.calls == []

    after = asyncio.run(protocol._handle_tools_list({}))
    after_names = {tool["name"] for tool in after["tools"]}
    assert "codex_resume" in after_names
    assert "read_codex_session" in after_names


def test_tool_mode_switch_rejects_invalid_mode():
    protocol = MCPProtocol({"app": {"tool_mode": "worker"}}, DummyToolHandler())

    response = asyncio.run(
        protocol.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "codex_tool_mode_switch",
                    "arguments": {"mode": "everything"},
                },
            }
        )
    )

    assert response["error"]["code"] == -32602
    assert "Invalid value for argument 'mode'" in response["error"]["message"]


def test_tool_call_returns_redacted_structured_content():
    secret_value = "fixture-" + "secret-value"

    class SecretToolHandler:
        async def handle_tool_call(self, tool_name, arguments):
            return {"output": f"token={secret_value}"}

    protocol = MCPProtocol({}, SecretToolHandler())

    result = asyncio.run(
        protocol._handle_tools_call(
            {
                "name": "codex_get_config",
                "arguments": {},
            }
        )
    )

    assert result["structuredContent"] == {"output": "token=[REDACTED_POSSIBLE_SECRET]"}
    assert secret_value not in result["content"][0]["text"]


def test_resume_tool_call_returns_async_job_pointer():
    class ResumeToolHandler:
        async def handle_tool_call(self, tool_name, arguments):
            assert tool_name == "codex_resume"
            assert arguments == {"session_id": "session-123", "prompt": "continue"}
            return {
                "job_id": "job-123",
                "mode": "resume",
                "session_id": "session-123",
                "status": "Operation initiated successfully",
            }

    protocol = MCPProtocol({}, ResumeToolHandler())

    result = asyncio.run(
        protocol._handle_tools_call(
            {
                "name": "codex_resume",
                "arguments": {"session_id": "session-123", "spec": "continue"},
            }
        )
    )

    assert result["structuredContent"]["operation_type"] == "codex_resume"
    assert result["structuredContent"]["job_id"] == "job-123"
    assert result["structuredContent"]["session_id"] == "session-123"
    assert "codex_get_status" in result["structuredContent"]["note"]


def test_resources_list_exposes_tool_card_template():
    protocol = MCPProtocol({}, DummyToolHandler())

    result = asyncio.run(protocol._handle_resources_list({}))

    assert result["resources"] == [
        {
            "uri": TOOL_CARD_URI,
            "name": "codex-mcp-wrapper-tool-card",
            "title": "Codex MCP Wrapper Tool Card",
            "description": "Compact ChatGPT Apps card for Codex MCP Wrapper tool results.",
            "mimeType": TOOL_CARD_MIME_TYPE,
        }
    ]


def test_resources_read_returns_apps_widget_resource():
    protocol = MCPProtocol({}, DummyToolHandler())

    result = asyncio.run(protocol._handle_resources_read({"uri": TOOL_CARD_URI}))

    content = result["contents"][0]
    assert content["uri"] == TOOL_CARD_URI
    assert content["mimeType"] == TOOL_CARD_MIME_TYPE
    assert "ui/notifications/tool-result" in content["text"]
    assert content["_meta"]["ui"]["prefersBorder"] is True
    assert content["_meta"]["ui"]["csp"] == {"connectDomains": [], "resourceDomains": []}
    assert content["_meta"]["openai/widgetPrefersBorder"] is True
    assert content["_meta"]["openai/widgetCSP"] == {"connect_domains": [], "resource_domains": []}


def test_resources_read_rejects_unknown_or_missing_uri():
    protocol = MCPProtocol({}, DummyToolHandler())

    for params in [{}, {"uri": "ui://widget/unknown.html"}]:
        response = asyncio.run(
            protocol.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "resources/read",
                    "params": params,
                }
            )
        )
        assert response["error"]["code"] == -32602


def test_initialize_can_complete_while_worker_inspect_tool_call_is_waiting():
    async def scenario():
        class SlowWorkerHandler:
            def __init__(self):
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def handle_tool_call(self, tool_name, arguments):
                assert tool_name == "codex_worker_inspect"
                self.started.set()
                await self.release.wait()
                return {"state": "working", "worker": arguments["worker"]}

        handler = SlowWorkerHandler()
        protocol = MCPProtocol({"app": {"tool_mode": "worker"}}, handler)
        inspect_task = asyncio.create_task(
            protocol.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "codex_worker_inspect",
                        "arguments": {"worker": "Slow Worker", "wait_seconds": 30},
                    },
                }
            )
        )
        await asyncio.wait_for(handler.started.wait(), timeout=0.2)

        initialize_response = await asyncio.wait_for(
            protocol.handle_message(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-11-25"},
                }
            ),
            timeout=0.2,
        )

        assert initialize_response["id"] == 2
        assert initialize_response["result"]["serverInfo"]["name"] == "codex-mcp-wrapper"

        handler.release.set()
        inspect_response = await asyncio.wait_for(inspect_task, timeout=0.2)
        assert inspect_response["id"] == 1
        assert inspect_response["result"]["structuredContent"]["state"] == "working"

    asyncio.run(scenario())
