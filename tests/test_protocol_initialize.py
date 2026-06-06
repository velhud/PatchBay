import asyncio

from mcp_protocol import MCPProtocol


class DummyToolHandler:
    async def handle_tool_call(self, tool_name, arguments):
        return {"tool_name": tool_name, "arguments": arguments}


def test_initialize_includes_server_instructions():
    protocol = MCPProtocol({}, DummyToolHandler())
    result = asyncio.run(protocol._handle_initialize({"protocolVersion": "2025-11-25"}))

    assert result["serverInfo"]["name"] == "codex-mcp-wrapper"
    assert result["serverInfo"]["version"] == "0.1.0"
    assert "instructions" in result
    assert "allowed roots" in result["instructions"]
