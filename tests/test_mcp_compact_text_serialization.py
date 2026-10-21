import asyncio
import json

import pytest

from patchbay.protocol.mcp import MCPProtocol


class PayloadToolHandler:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def handle_tool_call(self, tool_name, arguments, *, context=None):
        self.calls.append((tool_name, arguments))
        return self.payload


@pytest.mark.parametrize(
    ("tool_name", "arguments", "payload"),
    [
        (
            "codex_read_file",
            {"file_path": "src/example.py", "start_line": 1, "max_bytes": 4096},
            {
                "file_path": "src/example.py",
                "start_line": 1,
                "end_line": 8,
                "content": "def normalize(value):\n    return value.strip().lower()\n" * 12,
                "next_start_line": 9,
                "total_bytes": 712,
            },
        ),
        (
            "codex_get_status",
            {"job_id": "job-error-123"},
            {
                "job_id": "job-error-123",
                "state": "failed",
                "error": "The requested operation could not be completed.",
                "partial": True,
                "next_action": "inspect_result",
            },
        ),
        (
            "codex_get_result",
            {"job_id": "job-result-456"},
            {
                "job_id": "job-result-456",
                "state": "completed",
                "result": {
                    "record_id": "record-789",
                    "partial": True,
                    "next_cursor": "cursor-page-2",
                    "items": [{"source": "file.py", "line": 17, "value": "kept exactly"}],
                },
            },
        ),
    ],
)
def test_non_worker_mcp_text_is_compact_lossless_and_keeps_structured_result(
    tool_name, arguments, payload
):
    handler = PayloadToolHandler(payload)
    protocol = MCPProtocol(
        {
            "app": {"tool_mode": "full"},
            "power_tools": {
                "direct_write": True,
                "bash_mode": "full",
                "codex_session_read": True,
            },
        },
        handler,
    )

    response = asyncio.run(protocol._handle_tools_call({"name": tool_name, "arguments": arguments}))

    text = response["content"][0]["text"]
    assert handler.calls == [(tool_name, arguments)]
    assert response["structuredContent"] == payload
    assert json.loads(text) == response["structuredContent"]
    assert text == json.dumps(payload, separators=(",", ":"))
    assert len(text.encode("utf-8")) < len(json.dumps(payload, indent=2).encode("utf-8"))


def test_manager_guidance_is_initialized_without_reinserting_it_in_tool_results():
    protocol = MCPProtocol({}, PayloadToolHandler({"ok": True}))
    initialized = asyncio.run(protocol._handle_initialize({"protocolVersion": "2025-11-25"}))
    instructions = initialized["instructions"]
    assert instructions.count("raw MCP session ids") == 1
    assert instructions.count("Quiet work and wait timeouts") == 1
    for _ in range(2):
        response = asyncio.run(protocol._handle_tools_call({"name": "codex_self_test", "arguments": {}}))
        assert response["structuredContent"] == {"ok": True}
        assert "instructions" not in response
        assert instructions not in json.dumps(response)
