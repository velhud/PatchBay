from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any, Mapping

from patchbay.hub.operations import public_envelope
from patchbay.hub.protocol_v2 import (
    HUB_V2_INSTRUCTIONS,
    HUB_V2_PROTOCOL_METADATA,
    HubProtocolV2,
)
from patchbay.hub.tool_surface import (
    HUB_V1_ONLY_TOOL_NAMES,
    HUB_V2_ACTION_CAPABILITY_VERSION,
    HUB_V2_CONTRACT_HASH,
    HUB_V2_CONTRACT_VERSION,
    HUB_V2_EXPECTED_TOOL_COUNT,
    HUB_V2_MANIFEST_HASH,
    HUB_V2_SCHEMA_HASH,
    HUB_V2_TOOL_NAMES,
    HUB_V2_TOOLS_BY_NAME,
    get_hub_v2_tools,
)
from patchbay.protocol.context import RequestContext


class RecordingHandler:
    def __init__(self, output: Mapping[str, Any] | None = None, *, error: Exception | None = None):
        self.output = deepcopy(dict(output or public_envelope("ok")))
        self.error = error
        self.calls: list[tuple[str, dict[str, Any], RequestContext | None]] = []

    async def handle_tool_call(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        context: RequestContext | None = None,
    ) -> Mapping[str, Any]:
        self.calls.append((name, deepcopy(dict(arguments)), context))
        if self.error is not None:
            raise self.error
        return deepcopy(self.output)


def _request(protocol: HubProtocolV2, method: str, params: Mapping[str, Any], *, msg_id: int = 1):
    return asyncio.run(
        protocol.handle_message(
            {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": dict(params)}
        )
    )


def _tool_call(protocol: HubProtocolV2, name: str, arguments: Any, *, msg_id: int = 1):
    return _request(
        protocol,
        "tools/call",
        {"name": name, "arguments": arguments},
        msg_id=msg_id,
    )


def test_initialize_is_manager_first_and_carries_frozen_contract_metadata():
    protocol = HubProtocolV2(RecordingHandler())
    response = _request(
        protocol,
        "initialize",
        {"protocolVersion": "2025-11-25"},
    )

    result = response["result"]
    assert result["serverInfo"] == {
        "name": "patchbay-hub",
        "title": "PatchBay Hub V2",
        "version": "0.1.0",
    }
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["_meta"] == HUB_V2_PROTOCOL_METADATA
    assert result["_meta"] == {
        "patchbay/contract_version": HUB_V2_CONTRACT_VERSION,
        "patchbay/action_capability_version": HUB_V2_ACTION_CAPABILITY_VERSION,
        "patchbay/tool_count": 31,
        "patchbay/manifest_hash": HUB_V2_MANIFEST_HASH,
        "patchbay/schema_hash": HUB_V2_SCHEMA_HASH,
        "patchbay/contract_hash": HUB_V2_CONTRACT_HASH,
    }
    assert "Start with patchbay_fleet_status and patchbay_workspace_list" in HUB_V2_INSTRUCTIONS
    assert "One user task equals one work group" in HUB_V2_INSTRUCTIONS
    assert "patchbay_work_group_list -> patchbay_work_group_resume" in HUB_V2_INSTRUCTIONS
    assert "patchbay_work_group_create" in HUB_V2_INSTRUCTIONS
    assert "patchbay_work_group_status" in HUB_V2_INSTRUCTIONS
    assert "patchbay_work_group_close" in HUB_V2_INSTRUCTIONS
    assert "idempotency_key" in HUB_V2_INSTRUCTIONS
    assert "Reuse that same key" in HUB_V2_INSTRUCTIONS
    assert "20-30 seconds" in HUB_V2_INSTRUCTIONS
    assert "pending means" in HUB_V2_INSTRUCTIONS
    assert "patchbay_operation_status" in HUB_V2_INSTRUCTIONS
    assert "applies without committing" in HUB_V2_INSTRUCTIONS
    assert "manager, architect, and team lead" in HUB_V2_INSTRUCTIONS
    assert "Brief workers as real colleagues" in HUB_V2_INSTRUCTIONS
    assert "task and product purpose" in HUB_V2_INSTRUCTIONS
    assert "GPT-5.6 Luna" in HUB_V2_INSTRUCTIONS
    assert "GPT-5.6 Terra" in HUB_V2_INSTRUCTIONS
    assert "GPT-5.6 Sol" in HUB_V2_INSTRUCTIONS
    assert "use medium as the normal default" in HUB_V2_INSTRUCTIONS
    assert "5-10x" in HUB_V2_INSTRUCTIONS
    assert "choose Spark first" in HUB_V2_INSTRUCTIONS
    assert "immediately continue or retry the same assignment" in HUB_V2_INSTRUCTIONS
    assert "preserve the lane and record the fallback" in HUB_V2_INSTRUCTIONS
    assert "0.144.1 exposes ultra" in HUB_V2_INSTRUCTIONS
    assert "tool-call" in HUB_V2_INSTRUCTIONS
    assert "continuation note" in HUB_V2_INSTRUCTIONS
    assert "Waiting for healthy workers is part of executing the task" in HUB_V2_INSTRUCTIONS
    assert "Never claim an execution/tool-call limit" in HUB_V2_INSTRUCTIONS
    assert "Group close is complete only" in HUB_V2_INSTRUCTIONS


def test_tools_list_is_the_exact_ordered_31_tool_registry_with_no_v1_only_tools():
    protocol = HubProtocolV2(RecordingHandler())
    response = _request(protocol, "tools/list", {})

    tools = response["result"]["tools"]
    names = tuple(tool["name"] for tool in tools)
    assert tools == get_hub_v2_tools()
    assert len(tools) == HUB_V2_EXPECTED_TOOL_COUNT == 31
    assert names == HUB_V2_TOOL_NAMES
    assert not set(names).intersection(HUB_V1_ONLY_TOOL_NAMES)
    assert response["result"]["_meta"] == HUB_V2_PROTOCOL_METADATA


def test_tools_list_preserves_annotations_and_output_schemas_exactly():
    protocol = HubProtocolV2(RecordingHandler())
    tools = _request(protocol, "tools/list", {})["result"]["tools"]

    for listed in tools:
        source = HUB_V2_TOOLS_BY_NAME[listed["name"]]
        assert listed["annotations"] == source["annotations"]
        assert listed["readOnlyHint"] is source["readOnlyHint"]
        assert listed["outputSchema"] == source["outputSchema"]
        assert listed["outputSchema"]["additionalProperties"] is False
        assert set(listed["outputSchema"]["required"]) == {
            "status",
            "result",
            "operation",
            "warnings",
            "next_actions",
        }


def test_strict_descriptor_validation_rejects_unknown_missing_and_wrong_typed_inputs():
    handler = RecordingHandler()
    protocol = HubProtocolV2(handler)

    unknown = _tool_call(protocol, "patchbay_fleet_status", {"invented": True})
    missing = _tool_call(
        protocol,
        "patchbay_work_group_create",
        {"title": "Build", "goal": "Ship V2"},
    )
    wrong_type = _tool_call(protocol, "patchbay_workspace_list", {"max_results": True})
    non_object = _tool_call(protocol, "patchbay_fleet_status", ["not", "an", "object"])

    assert unknown["error"]["code"] == -32602
    assert "unknown field" in unknown["error"]["message"]
    assert missing["error"]["code"] == -32602
    assert "idempotency_key" in missing["error"]["message"]
    assert wrong_type["error"]["code"] == -32602
    assert "expected integer" in wrong_type["error"]["message"]
    assert non_object["error"]["code"] == -32602
    assert handler.calls == []


def test_strict_descriptor_validation_enforces_nested_anyof_and_cleanup_condition():
    handler = RecordingHandler()
    protocol = HubProtocolV2(handler)

    missing_route = _tool_call(
        protocol,
        "patchbay_worker_start",
        {
            "name": "Implementer",
            "brief": "Implement the protocol.",
            "idempotency_key": "start-1",
        },
    )
    unsafe_cleanup = _tool_call(
        protocol,
        "patchbay_worker_stop",
        {
            "work_group_id": "group-1",
            "worker": "Implementer",
            "cleanup_workspace": True,
            "idempotency_key": "stop-1",
        },
    )

    assert missing_route["error"]["code"] == -32602
    assert "allowed schema" in missing_route["error"]["message"]
    assert unsafe_cleanup["error"]["code"] == -32602
    assert "discard_unintegrated_changes" in unsafe_cleanup["error"]["message"]
    assert handler.calls == []


def test_validated_call_dispatches_public_tool_name_and_arguments_to_injected_handler():
    handler = RecordingHandler(public_envelope("ok", result={"worker": {"name": "Implementer"}}))
    protocol = HubProtocolV2(handler)
    context = RequestContext(client_ref="client-test", work_group_id="group-1")
    arguments = {
        "work_group_id": "group-1",
        "lane": "implementation",
        "name": "Implementer",
        "brief": "Implement the protocol.",
        "idempotency_key": "start-1",
    }

    response = asyncio.run(
        protocol.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {"name": "patchbay_worker_start", "arguments": arguments},
            },
            context=context,
        )
    )

    assert "error" not in response
    assert handler.calls == [("patchbay_worker_start", arguments, context)]
    assert response["result"]["structuredContent"]["status"] == "ok"
    assert response["result"]["_meta"]["patchbay/tool_name"] == "patchbay_worker_start"


def test_startup_tools_have_identifier_rich_text_fallbacks():
    assert "machine_alpha=online" in HubProtocolV2._compact_text(
        "patchbay_fleet_status",
        public_envelope("ok", result={"machines": [{"machine_id": "machine_alpha", "status": "online"}]}),
    )
    assert "workspace_archive" in HubProtocolV2._compact_text(
        "patchbay_workspace_list",
        public_envelope("ok", result={"workspaces": [{"workspace_ref": "workspace_archive", "projections": []}]}),
    )
    assert "group_archive" in HubProtocolV2._compact_text(
        "patchbay_work_group_list",
        public_envelope("ok", result={"work_groups": [{"work_group_id": "group_archive", "status": "open", "title": "Archive"}]}),
    )
    assert "gpt-test" in HubProtocolV2._compact_text(
        "patchbay_worker_options",
        public_envelope("ok", result={"models": [{"id": "gpt-test"}], "default_model": "gpt-test"}),
    )


def test_nullable_workspace_and_worker_fields_survive_actual_mcp_framing():
    workspace = HubProtocolV2(
        RecordingHandler(public_envelope("ok", result={"workspace_id": "workspace_archive", "tree": None}))
    )
    worker = HubProtocolV2(
        RecordingHandler(
            public_envelope(
                "ok",
                result={
                    "workers": [
                        {
                            "worker_id": "worker_archive",
                            "name": "Archive verifier",
                            "last_activity_at": None,
                        }
                    ]
                },
            )
        )
    )

    workspace_response = _tool_call(
        workspace,
        "patchbay_workspace_open",
        {"work_group_id": "group_archive"},
    )
    worker_response = _tool_call(
        worker,
        "patchbay_worker_status",
        {"work_group_id": "group_archive"},
    )

    assert workspace_response["result"]["structuredContent"]["result"]["tree"] is None
    assert worker_response["result"]["structuredContent"]["result"]["workers"][0]["last_activity_at"] is None


def test_pending_envelope_passes_through_without_queue_receipt_fabrication_or_text_duplication():
    large_report = "x" * 20_000
    pending = public_envelope(
        "pending",
        result={"summary": "Waiting for the real Edge domain result.", "report": large_report},
        operation={
            "operation_id": "op-123",
            "tool_name": "patchbay_work_group_create",
            "state": "running",
            "idempotency_key": "group-create-1",
        },
        next_actions=[{"tool": "patchbay_operation_status", "arguments": {"operation_id": "op-123"}}],
    )
    handler = RecordingHandler(pending)
    protocol = HubProtocolV2(handler)

    response = _tool_call(
        protocol,
        "patchbay_work_group_create",
        {
            "title": "Hub V2",
            "goal": "Implement the manager protocol.",
            "idempotency_key": "group-create-1",
        },
    )

    result = response["result"]
    assert result["structuredContent"] == pending
    assert "accepted" not in result["structuredContent"]
    assert "queue" not in result["structuredContent"]
    text = result["content"][0]["text"]
    assert "pending" in text
    assert "op-123" in text
    assert "patchbay_operation_status" in text
    assert large_report not in text
    assert len(text) < 700


def test_blocked_domain_envelope_is_a_successful_mcp_result_not_an_mcp_error():
    blocked = public_envelope(
        "blocked",
        result={"reason": "active_turn_in_progress", "worker": {"name": "Implementer"}},
        operation={
            "operation_id": "op-message-1",
            "tool_name": "patchbay_worker_message",
            "state": "blocked",
        },
        warnings=[{"code": "active_turn", "message": "The current worker turn is still active."}],
        next_actions=["Wait for the active turn to complete."],
    )
    protocol = HubProtocolV2(RecordingHandler(blocked))

    response = _tool_call(
        protocol,
        "patchbay_worker_message",
        {
            "work_group_id": "group-1",
            "worker": "Implementer",
            "message": "Continue after the current turn.",
            "idempotency_key": "message-1",
        },
    )

    assert "error" not in response
    assert response["result"]["structuredContent"] == blocked
    assert response["result"]["structuredContent"]["status"] == "blocked"


def test_invalid_handler_output_and_handler_exceptions_are_internal_mcp_faults():
    receipt_only = HubProtocolV2(RecordingHandler({"accepted": True, "command_id": "cmd-1"}))
    bad_output = _tool_call(receipt_only, "patchbay_fleet_status", {})
    exploded = HubProtocolV2(RecordingHandler(error=ValueError("domain implementation exploded")))
    handler_error = _tool_call(exploded, "patchbay_fleet_status", {})

    assert bad_output == {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32603, "message": "Internal processing error"},
    }
    assert handler_error == {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32603, "message": "Internal processing error"},
    }


def test_python_only_input_and_output_values_are_rejected_at_the_json_boundary():
    protocol = HubProtocolV2(RecordingHandler())
    bad_input = _tool_call(protocol, "patchbay_fleet_status", {"tags": ("local",)})
    bad_output = HubProtocolV2(
        RecordingHandler(public_envelope("ok", result={"not_json": {"set-value"}}))
    )
    output_response = _tool_call(bad_output, "patchbay_fleet_status", {})

    assert bad_input["error"]["code"] == -32602
    assert "not JSON-compatible" in bad_input["error"]["message"]
    assert output_response["error"] == {
        "code": -32603,
        "message": "Internal processing error",
    }


def test_unknown_tool_and_method_are_protocol_errors_without_dispatch():
    handler = RecordingHandler()
    protocol = HubProtocolV2(handler)

    unknown_tool = _tool_call(protocol, "patchbay_machine_list", {})
    unknown_method = _request(protocol, "resources/list", {})

    assert unknown_tool["error"]["code"] == -32602
    assert unknown_method["error"]["code"] == -32601
    assert handler.calls == []
