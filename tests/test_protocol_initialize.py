import asyncio

from patchbay.protocol.context import RequestContext
from patchbay.protocol.mcp import MCPProtocol
from patchbay.protocol.resources import TOOL_CARD_MIME_TYPE, TOOL_CARD_URI


class DummyToolHandler:
    def __init__(self):
        self.calls = []
        self.contexts = []

    async def handle_tool_call(self, tool_name, arguments, *, context=None):
        self.calls.append((tool_name, arguments))
        self.contexts.append(context)
        return {"tool_name": tool_name, "arguments": arguments}


def full_power_config(mode="full"):
    return {
        "app": {"tool_mode": mode},
        "power_tools": {
            "direct_write": True,
            "bash_mode": "full",
            "codex_session_read": True,
        },
    }


def test_initialize_includes_server_instructions():
    protocol = MCPProtocol({}, DummyToolHandler())
    result = asyncio.run(protocol._handle_initialize({"protocolVersion": "2025-11-25"}))

    assert result["serverInfo"]["name"] == "patchbay"
    assert result["serverInfo"]["version"] == "0.1.0"
    assert "instructions" in result
    assert "manager, engineering lead, and coordinator" in result["instructions"]
    assert "not the primary repository file reader" in result["instructions"]
    assert "Which worker or worker team should I appoint?" in result["instructions"]
    assert "Direct read/search/git tools remain available" in result["instructions"]
    assert "up to 10 concurrent worker slots" in result["instructions"]
    assert "one shared local server" in result["instructions"]
    assert "repo_busy" in result["instructions"]
    assert "raw MCP session ids" in result["instructions"]
    assert "codex_self_test" in result["instructions"]
    assert "codex_open_workspace" in result["instructions"]
    assert "Use read-only context tools for light orientation" in result["instructions"]
    assert "communicate with them in normal engineering language" in result["instructions"]
    assert "split responsibilities across multiple isolated_write workers" in result["instructions"]
    assert "continuing specialists" in result["instructions"]
    assert "durable report file" in result["instructions"]
    assert "missing evidence" in result["instructions"]
    assert "Use codex_worker_message for those loops" in result["instructions"]
    assert "context_from_workers" in result["instructions"]
    assert "codex_worker_inbox(action=import_file)" in result["instructions"]
    assert "Workers are stateful by name" in result["instructions"]
    assert "integration_preview" in result["instructions"]
    assert "does not commit" in result["instructions"]
    assert "allowed roots" in result["instructions"]
    assert "--allow-root" in result["instructions"]
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


def test_public_tool_call_passes_request_context_to_handler():
    handler = DummyToolHandler()
    protocol = MCPProtocol({}, handler)
    context = RequestContext(
        transport_session_id="private-session-id",
        client_ref="client_abc123",
        tool_mode="full",
    )

    result = asyncio.run(
        protocol.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "codex_get_status",
                    "arguments": {"job_id": "job-123"},
                },
            },
            context=context,
        )
    )

    assert result["result"]["structuredContent"]["tool_name"] == "codex_get_status"
    assert handler.contexts == [context]
    assert "private-session-id" not in result["result"]["content"][0]["text"]


def test_public_tool_call_supports_legacy_handler_without_context():
    class LegacyToolHandler:
        def __init__(self):
            self.calls = []

        async def handle_tool_call(self, tool_name, arguments):
            self.calls.append((tool_name, arguments))
            return {"ok": True}

    handler = LegacyToolHandler()
    protocol = MCPProtocol({}, handler)

    result = asyncio.run(
        protocol._handle_tools_call(
            {
                "name": "codex_get_status",
                "arguments": {"job_id": "job-123"},
            },
            context=RequestContext(transport_session_id="private-session-id", client_ref="client_abc123"),
        )
    )

    assert result["structuredContent"] == {"ok": True}
    assert handler.calls == [("codex_get_status", {"job_id": "job-123"})]


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
    protocol = MCPProtocol(full_power_config("worker"), handler)

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
    assert payload["switch_scope"] == "process"
    assert "config files were not modified" in payload["note"]
    assert "Refresh" in payload["chatgpt_refresh_note"]
    assert handler.calls == []

    after = asyncio.run(protocol._handle_tools_list({}))
    after_names = {tool["name"] for tool in after["tools"]}
    assert "codex_resume" in after_names
    assert "read_codex_session" in after_names


def test_tool_mode_switch_is_session_local_with_request_context():
    handler = DummyToolHandler()
    config = full_power_config("worker")
    protocol = MCPProtocol(config, handler)
    session_a_data = {}
    session_b_data = {}
    context_a = RequestContext.from_session("session-a", session_a_data, salt="test-salt")
    context_b = RequestContext.from_session("session-b", session_b_data, salt="test-salt")

    before_a = asyncio.run(protocol._handle_tools_list({}, context=context_a))
    before_b = asyncio.run(protocol._handle_tools_list({}, context=context_b))
    assert "codex_resume" not in {tool["name"] for tool in before_a["tools"]}
    assert "codex_resume" not in {tool["name"] for tool in before_b["tools"]}

    switched = asyncio.run(
        protocol._handle_tools_call(
            {
                "name": "codex_tool_mode_switch",
                "arguments": {"mode": "full", "reason": "Need raw session continuation."},
            },
            context=context_a,
        )
    )

    payload = switched["structuredContent"]
    assert payload["previous_mode"] == "worker"
    assert payload["current_mode"] == "full"
    assert payload["default_mode"] == "worker"
    assert payload["switch_scope"] == "session"
    assert session_a_data["tool_mode"] == "full"
    assert "tool_mode" not in session_b_data
    assert config["app"]["tool_mode"] == "worker"

    after_a = asyncio.run(protocol._handle_tools_list({}, context=context_a))
    after_b = asyncio.run(protocol._handle_tools_list({}, context=context_b))
    assert "codex_resume" in {tool["name"] for tool in after_a["tools"]}
    assert "codex_resume" not in {tool["name"] for tool in after_b["tools"]}

    info_a = asyncio.run(
        protocol._handle_tools_call(
            {"name": "codex_tool_mode_info", "arguments": {}},
            context=context_a,
        )
    )
    info_b = asyncio.run(
        protocol._handle_tools_call(
            {"name": "codex_tool_mode_info", "arguments": {}},
            context=context_b,
        )
    )
    assert info_a["structuredContent"]["current_mode"] == "full"
    assert info_b["structuredContent"]["current_mode"] == "worker"


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


def test_tool_call_value_error_redacts_public_message():
    secret_value = "fixture-" + "secret-value"

    class PathErrorToolHandler:
        async def handle_tool_call(self, tool_name, arguments):
            raise ValueError(f"Path is outside configured allowed roots: /Users/example/private token={secret_value}")

    protocol = MCPProtocol({}, PathErrorToolHandler())

    response = asyncio.run(
        protocol.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "codex_get_status",
                    "arguments": {"job_id": "job-123"},
                },
            }
        )
    )

    assert response["error"]["code"] == -32602
    assert response["error"]["message"] == "Path is outside configured allowed roots"
    assert "/Users" not in response["error"]["message"]
    assert secret_value not in response["error"]["message"]


def test_tool_call_unexpected_error_is_generic():
    secret_value = "sk-" + ("x" * 24)

    class RuntimeErrorToolHandler:
        async def handle_tool_call(self, tool_name, arguments):
            raise RuntimeError(f"boom /Users/example/private {secret_value}")

    protocol = MCPProtocol({}, RuntimeErrorToolHandler())

    response = asyncio.run(
        protocol.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "codex_get_status",
                    "arguments": {"job_id": "job-123"},
                },
            }
        )
    )

    assert response["error"]["code"] == -32603
    assert response["error"]["message"] == "Internal processing error"
    assert "/Users" not in response["error"]["message"]
    assert secret_value not in response["error"]["message"]


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
                "name": "patchbay-tool-card",
                "title": "PatchBay Tool Card",
                "description": "Rich ChatGPT Apps card for PatchBay worker, artifact, job, diff, and power-tool results.",
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
        assert initialize_response["result"]["serverInfo"]["name"] == "patchbay"

        handler.release.set()
        inspect_response = await asyncio.wait_for(inspect_task, timeout=0.2)
        assert inspect_response["id"] == 1
        assert inspect_response["result"]["structuredContent"]["state"] == "working"

    asyncio.run(scenario())
