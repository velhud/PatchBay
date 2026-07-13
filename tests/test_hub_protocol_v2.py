from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from patchbay.hub.app_v2 import HubAppV2
from patchbay.hub.broker import OperationBroker
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
    HUB_V2_SECURITY_SCHEMES,
    HUB_V2_TOOL_NAMES,
    HUB_V2_TOOLS_BY_NAME,
    get_hub_v2_tools,
)
from patchbay.hub.runtime_v2 import HubRuntimeV2
from patchbay.hub.store_v2 import HubStoreV2
from patchbay.hub.transport_v2 import (
    EDGE_OUTCOME_UNKNOWN_REASON,
    HubPullTransportBridgeV2,
    edge_reconciliation_requests,
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


@pytest.mark.asyncio
async def test_protocol_reports_ready_unclaimed_work_as_active_without_recovery(
    tmp_path: Path,
) -> None:
    delivery = HubPullTransportBridgeV2()
    app = HubAppV2(tmp_path / "healthy-protocol.sqlite3", edge_delivery=delivery)
    delivery.bind(app)
    code = app.runtime.create_enrollment_code(name="Protocol Edge", tags=["codex"])[
        "code"
    ]
    enrolled = app.runtime.enroll_machine(
        code=code,
        machine_id="machine-protocol",
        edge_generation="generation-protocol",
        display_name="Protocol Edge",
        tags=["codex"],
    )
    app.runtime.heartbeat(
        machine_id="machine-protocol",
        token=enrolled["node_token"],
        edge_generation="generation-protocol",
        projection_revision=1,
        capabilities={
            "contract_hash": HUB_V2_CONTRACT_HASH,
            "action_capabilities": {
                "codex_open_workspace": "2",
                "codex_worker_start": "2",
            },
            "action_capability_versions": {
                "codex_open_workspace": "2",
                "codex_worker_start": "2",
            },
            "max_concurrent_jobs": 2,
            "queue_enabled": True,
        },
        workspaces=[
            {
                "workspace_ref": "workspace-protocol",
                "alias": "protocol-repo",
                "path": "/tmp/patchbay-protocol-repo",
                "exists": True,
                "git": True,
            }
        ],
        resource_status={"active_workers": 0, "free_worker_slots": 2},
    )
    context = RequestContext(
        client_ref="protocol-client",
        owner_ref="protocol-owner",
        chatgpt_session_ref="protocol-session",
    )

    async def call(
        name: str, arguments: Mapping[str, Any], request_id: int
    ) -> dict[str, Any]:
        response = await app.protocol.handle_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": dict(arguments)},
            },
            context=context,
        )
        assert response is not None and "error" not in response, response
        return deepcopy(response["result"]["structuredContent"])

    created = await call(
        "patchbay_work_group_create",
        {
            "title": "Healthy protocol group",
            "goal": "Keep ordinary dispatch separate from recovery.",
            "repo_path": "protocol-repo",
            "lanes": [{"lane": "main", "title": "Main", "role": "Implement"}],
            "idempotency_key": "healthy-protocol-group",
        },
        1,
    )
    group_id = str(created["result"]["work_group"]["work_group_id"])
    group_entity = app.store.get_entity("hub.work_group", group_id)
    assert group_entity is not None
    preflight_id = str(group_entity["record"]["readiness"]["operation_id"])
    app.runtime.record_preflight_result(
        work_group_id=group_id,
        operation_id=preflight_id,
        result={
            "ok": True,
            "repo_exists": True,
            "repo_resolved": "/tmp/patchbay-protocol-repo",
            "disk_free_bytes": 10_000_000_000,
            "free_worker_slots": 2,
            "queue_enabled": True,
        },
    )

    started = await call(
        "patchbay_worker_start",
        {
            "work_group_id": group_id,
            "lane": "main",
            "name": "Healthy Worker",
            "brief": "Remain ordinary active work while awaiting the Edge claim.",
            "idempotency_key": "healthy-protocol-worker",
        },
        2,
    )
    status = await call(
        "patchbay_work_group_status", {"work_group_id": group_id}, 3
    )
    operation_id = str(started["operation"]["operation_id"])
    operation = app.store.get_operation(operation_id)
    attempt_row = app.store.connection.execute(
        "SELECT attempt_id FROM attempts WHERE operation_id = ?",
        (operation_id,),
    ).fetchone()
    assert operation is not None and attempt_row is not None
    attempt = app.store.get_attempt(str(attempt_row["attempt_id"]))
    counts = status["result"]["completion_contract"]["activity_counts"]

    assert started["status"] == "pending"
    assert operation["state"] == "running"
    assert attempt is not None and attempt["state"] == "offered"
    assert status["result"]["readiness"]["status"] == "ready"
    assert status["result"]["work_group"]["activity"] == "active"
    assert status["result"]["completion_contract"]["reason"] == "operations_active"
    assert status["result"]["completion_contract"]["recommended_next_action"][
        "tool"
    ] == "patchbay_work_group_status"
    assert counts["active_operations"] == 1
    assert counts["uncertain_operations"] == 0
    generation = delivery._generation_number("generation-protocol")
    assert edge_reconciliation_requests(
        app.store, "machine-protocol", generation
    ) == []
    app.close()


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
    assert len(HUB_V2_INSTRUCTIONS) < 5_000
    assert "manager,\narchitect, and team lead" in HUB_V2_INSTRUCTIONS
    assert "One task is one group" in HUB_V2_INSTRUCTIONS
    assert "execution_mode=end_to_end" in HUB_V2_INSTRUCTIONS
    assert "asynchronous_handoff" in HUB_V2_INSTRUCTIONS
    assert "completion_contract" in HUB_V2_INSTRUCTIONS
    assert "final_response_allowed=false" in HUB_V2_INSTRUCTIONS
    assert "do not produce a voluntary final answer" in HUB_V2_INSTRUCTIONS
    assert "20-30 second intervals" in HUB_V2_INSTRUCTIONS
    assert "exactly 31 PatchBay tools" in HUB_V2_INSTRUCTIONS
    assert "connector manifest is stale or partial" in HUB_V2_INSTRUCTIONS
    assert "timeout means" in HUB_V2_INSTRUCTIONS
    assert "Never invent a tool-call" in HUB_V2_INSTRUCTIONS
    assert "complete callable tool action" in HUB_V2_INSTRUCTIONS
    assert "natural-language manager" in HUB_V2_INSTRUCTIONS
    assert "never fabricate missing judgment" in HUB_V2_INSTRUCTIONS
    assert "patchbay_worker_start_batch" in HUB_V2_INSTRUCTIONS
    assert "batch parent is Hub aggregate work" in HUB_V2_INSTRUCTIONS
    assert "cleanup_pending" in HUB_V2_INSTRUCTIONS
    assert "patchbay_worker_message" in HUB_V2_INSTRUCTIONS
    assert "patchbay_worker_options" in HUB_V2_INSTRUCTIONS
    assert "Spark" in HUB_V2_INSTRUCTIONS
    assert "GPT-5.4 Mini" in HUB_V2_INSTRUCTIONS
    assert "Luna" in HUB_V2_INSTRUCTIONS
    assert "Terra" in HUB_V2_INSTRUCTIONS
    assert "Sol medium is the normal default" in HUB_V2_INSTRUCTIONS
    assert "leave active work running" not in HUB_V2_INSTRUCTIONS
    assert "continuation-state packet" in HUB_V2_INSTRUCTIONS
    assert "repository and revision" in HUB_V2_INSTRUCTIONS
    assert "integration/commit/push state" in HUB_V2_INSTRUCTIONS


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
        assert listed["securitySchemes"] == HUB_V2_SECURITY_SCHEMES
        assert listed["_meta"]["securitySchemes"] == HUB_V2_SECURITY_SCHEMES
        assert listed["outputSchema"] == source["outputSchema"]
        assert listed["outputSchema"]["additionalProperties"] is False
        assert set(listed["outputSchema"]["required"]) == {
            "status",
            "result",
            "operation",
            "warnings",
            "next_actions",
        }


def test_worker_monitoring_descriptors_expose_enforced_hub_cadence():
    for name in (
        "patchbay_worker_list",
        "patchbay_worker_status",
        "patchbay_worker_wait",
    ):
        descriptor = HUB_V2_TOOLS_BY_NAME[name]
        assert "20-second" in descriptor["description"]
        assert "30-second" in descriptor["description"]


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        (
            "patchbay_worker_list",
            {
                "work_group_id": "group-monitoring",
                "lane": "implementation",
                "active_only": True,
                "include_stopped": False,
                "cursor": "0",
                "limit": 1,
            },
        ),
        (
            "patchbay_worker_status",
            {
                "work_group_id": "group-monitoring",
                "lane": "implementation",
                "active_only": True,
                "include_stopped": False,
                "cursor": "0",
                "limit": 1,
                "since_revision": 0,
            },
        ),
        (
            "patchbay_worker_wait",
            {
                "work_group_id": "group-monitoring",
                "lane": "implementation",
                "active_only": True,
                "include_stopped": False,
                "cursor": "0",
                "limit": 1,
                "since_revision": 0,
                "wait_seconds": 20,
            },
        ),
    ],
)
def test_group_worker_monitoring_protocol_accepts_every_advertised_input(
    name: str, arguments: Mapping[str, Any]
) -> None:
    response = _tool_call(HubProtocolV2(RecordingHandler()), name, arguments)

    assert "error" not in response


@pytest.mark.parametrize(
    "field,value",
    [
        ("scope", "current"),
        ("owned_only", True),
        ("created_after", 1),
        ("repo_path", "single-machine-only"),
        ("force_refresh", True),
    ],
)
@pytest.mark.parametrize(
    "name",
    ("patchbay_worker_list", "patchbay_worker_status", "patchbay_worker_wait"),
)
def test_group_worker_monitoring_protocol_rejects_unsupported_filters(
    name: str, field: str, value: Any
) -> None:
    response = _tool_call(
        HubProtocolV2(RecordingHandler()),
        name,
        {"work_group_id": "group-monitoring", field: value},
    )

    assert response["error"]["code"] == -32602


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


def test_protocol_rejects_next_action_tools_outside_the_exact_hub_registry():
    protocol = HubProtocolV2(
        RecordingHandler(
            public_envelope(
                "pending",
                next_actions=[{"tool": "complete_reconciliation"}],
            )
        )
    )

    response = _tool_call(protocol, "patchbay_fleet_status", {})

    assert response == {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32603, "message": "Internal processing error"},
    }
    assert "complete_reconciliation" not in str(response)


def test_protocol_rejects_known_next_action_with_incomplete_arguments():
    protocol = HubProtocolV2(
        RecordingHandler(
            public_envelope(
                "pending",
                next_actions=[
                    {
                        "tool": "patchbay_worker_wait",
                        "arguments": {"wait_seconds": 30},
                    }
                ],
            )
        )
    )

    response = _tool_call(protocol, "patchbay_fleet_status", {})

    assert response == {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32603, "message": "Internal processing error"},
    }


def test_group_close_protocol_requires_explicit_discard_consent_and_rejects_old_side_effect_inputs():
    handler = RecordingHandler()
    protocol = HubProtocolV2(handler)
    base = {
        "work_group_id": "group-close",
        "outcome": "abandoned",
        "summary": "Record the discarded worker result.",
        "worker_dispositions": [{"worker": "Writer", "disposition": "discarded"}],
        "idempotency_key": "close-discard-001",
    }
    no_consent = _tool_call(protocol, "patchbay_work_group_close", base)
    accepted_disposition = {
        **base,
        "worker_dispositions": [
            {
                "worker": "Writer",
                "disposition": "discarded",
                "discard_unintegrated_changes": True,
            }
        ],
    }
    old_stop = _tool_call(
        protocol,
        "patchbay_work_group_close",
        {**accepted_disposition, "active_work_disposition": "stop"},
    )
    old_cleanup = _tool_call(
        protocol,
        "patchbay_work_group_close",
        {**accepted_disposition, "cleanup_completed_workspaces": True},
    )
    accepted = _tool_call(
        protocol,
        "patchbay_work_group_close",
        accepted_disposition,
    )

    assert no_consent["error"]["code"] == -32602
    assert old_stop["error"]["code"] == -32602
    assert old_cleanup["error"]["code"] == -32602
    assert "error" not in accepted


def test_legacy_batch_recovery_status_is_valid_real_protocol_output(tmp_path):
    store = HubStoreV2(tmp_path / "legacy-recovery.sqlite3")
    broker = OperationBroker(store)
    parent = broker.create_operation(
        tool="patchbay_worker_start_batch",
        logical_target="group-legacy",
        idempotency_key="legacy-recovery",
        payload={"items": ["reader", "writer"]},
    )
    broker.declare_child_manifest(
        parent["operation_id"], expected_item_ids=["reader", "writer"]
    )
    broker.create_child_operation(
        parent["operation_id"],
        item_id="reader",
        tool="patchbay_worker_start",
        logical_target="group-legacy/reader",
        payload={"name": "Reader"},
    )

    class BrokerStatusHandler:
        async def handle_tool_call(self, name, arguments, *, context=None):
            assert name == "patchbay_operation_status"
            return await broker.operation_status(arguments["operation_id"])

    response = _tool_call(
        HubProtocolV2(BrokerStatusHandler()),
        "patchbay_operation_status",
        {"operation_id": parent["operation_id"]},
    )

    assert "error" not in response
    output = response["result"]["structuredContent"]
    assert output["status"] == "blocked"
    assert set(output["warnings"][0]) == {"code", "message", "details"}
    assert output["warnings"][0]["details"]["missing_item_ids"] == ["writer"]
    assert set(output["next_actions"][0]) == {"tool", "arguments", "reason"}
    assert output["next_actions"][0]["tool"] == "patchbay_operation_status"
    assert output["result"]["recovery"]["reason"] == "incomplete_atomic_child_set"
    assert output["operation"]["parent_operation_id"] == ""
    assert "event_revision" not in output["operation"]
    store.close()


def test_complete_batch_missing_dispatch_is_recovery_required_protocol_output(tmp_path):
    store = HubStoreV2(tmp_path / "legacy-missing-dispatch.sqlite3")
    broker = OperationBroker(store)
    child_specs = [
        {
            "item_id": item_id,
            "tool": "patchbay_worker_start",
            "logical_target": f"group-legacy/{item_id}",
            "payload": {
                "action": "codex_worker_start",
                "arguments": {"name": item_id.title()},
            },
        }
        for item_id in ("reader", "writer")
    ]
    batch = broker.create_batch_operation(
        logical_target="group-legacy",
        idempotency_key="legacy-missing-dispatch",
        payload={"items": ["reader", "writer"]},
        child_specs=child_specs,
        child_dispatch_specs=[
            {
                "item_id": spec["item_id"],
                "action": spec["payload"]["action"],
                "payload": spec["payload"],
            }
            for spec in child_specs
        ],
    )
    writer_operation_id = next(
        child["operation_id"]
        for child in batch["children"]
        if child["item_id"] == "writer"
    )
    with store.immediate_transaction() as connection:
        connection.execute(
            "DELETE FROM entity_records WHERE entity_type = ? AND entity_id = ?",
            ("hub.edge_dispatch", writer_operation_id),
        )

    class BrokerStatusHandler:
        async def handle_tool_call(self, name, arguments, *, context=None):
            assert name == "patchbay_operation_status"
            return await broker.operation_status(arguments["operation_id"])

    response = _tool_call(
        HubProtocolV2(BrokerStatusHandler()),
        "patchbay_operation_status",
        {"operation_id": batch["parent"]["operation_id"]},
    )

    assert "error" not in response
    output = response["result"]["structuredContent"]
    assert output["status"] == "blocked"
    assert output["result"]["dispatch"]["state"] == "recovery_required"
    assert output["result"]["safe_next_action"] == "inspect_and_replace_batch"
    assert output["result"]["recovery"] == {
        "reason": "incomplete_atomic_child_dispatch_set",
        "expected_item_ids": ["reader", "writer"],
        "actual_item_ids": ["reader", "writer"],
        "dispatched_item_ids": ["reader"],
        "missing_item_ids": ["writer"],
    }
    assert output["warnings"][0]["details"]["missing_item_ids"] == ["writer"]
    assert output["next_actions"][0]["tool"] == "patchbay_operation_status"
    assert output["next_actions"][0]["arguments"] == {
        "operation_id": batch["parent"]["operation_id"]
    }
    assert output["next_actions"][0]["reason"] != "wait_for_child_operations"
    store.close()


@pytest.mark.parametrize("local_state", ["executing", "effect_recorded"])
def test_hub_effect_boundary_reconciliation_blocks_once_and_is_idempotent(
    tmp_path: Path,
    local_state: str,
) -> None:
    store = HubStoreV2(tmp_path / f"hub-{local_state}.sqlite3")
    broker = OperationBroker(store)
    runtime = HubRuntimeV2(store, broker=broker)
    bridge = HubPullTransportBridgeV2(
        SimpleNamespace(store=store, broker=broker, runtime=runtime)
    )
    code = runtime.create_enrollment_code(name="Runtime Edge", tags=["codex"])[
        "code"
    ]
    enrolled = runtime.enroll_machine(
        code=code,
        machine_id="runtime-machine",
        edge_generation="runtime-generation",
        display_name="Runtime Edge",
        tags=["codex"],
    )
    capabilities = {
        "contract_hash": HUB_V2_CONTRACT_HASH,
        "action_capabilities": {"codex_worker_start": "2"},
        "action_capability_versions": {"codex_worker_start": "2"},
        "max_concurrent_jobs": 1,
        "queue_enabled": True,
    }
    runtime.heartbeat(
        machine_id="runtime-machine",
        token=enrolled["node_token"],
        edge_generation="runtime-generation",
        projection_revision=1,
        capabilities=capabilities,
        workspaces=[],
        resource_status={"active_workers": 0, "free_worker_slots": 1},
    )
    payload = {
        "action": "codex_worker_start",
        "arguments": {"name": "Builder", "brief": "Build", "repo_path": "repo"},
        "machine_id": "runtime-machine",
        "edge_generation": "runtime-generation",
        "target": {
            "machine_id": "runtime-machine",
            "edge_generation": "runtime-generation",
        },
    }
    operation = broker.create_operation(
        tool="patchbay_worker_start",
        logical_target=f"effect-boundary-{local_state}",
        idempotency_key=f"effect-boundary-{local_state}",
        payload=payload,
    )
    operation = broker.prepare_operation(
        operation["operation_id"], expected_revision=int(operation["revision"])
    )
    assert operation is not None
    operation = broker.make_dispatchable(
        operation["operation_id"], expected_revision=int(operation["revision"])
    )
    assert operation is not None
    dispatch = bridge._persist_dispatch(operation, payload)
    bridge._offer_dispatch(operation, dispatch)
    identity = {
        "machine_id": "runtime-machine",
        "edge_generation": "runtime-generation",
        "contract_hash": HUB_V2_CONTRACT_HASH,
    }
    claimed = bridge.edge_claim(
        {**identity, "available_slots": 1, "max_attempts": 1, "lease_seconds": 30},
        token=enrolled["node_token"],
    )["attempt"]
    executing = bridge.edge_lease(
        {
            **identity,
            "operation_id": claimed["operation_id"],
            "attempt_id": claimed["attempt_id"],
            "fencing_token": claimed["fencing_token"],
            "expected_revision": claimed["revision"],
            "lease_seconds": 30,
        },
        token=enrolled["node_token"],
    )["attempt"]
    recovery = {
        "found": True,
        "operation_id": operation["operation_id"],
        "attempt_id": executing["attempt_id"],
        "fencing_token": executing["fencing_token"],
        "state": local_state,
        "recovery_action": "reconcile_effect",
        "effect_started": True,
        "effect": {"domain_result_hash": "hash-without-durable-result"},
    }
    request = {
        **identity,
        "operation_id": operation["operation_id"],
        "attempt_id": executing["attempt_id"],
        "fencing_token": executing["fencing_token"],
        "local_recovery": recovery,
    }

    waiting = bridge.edge_reconcile(request, token=enrolled["node_token"])
    broker.expire_leases(now=float(executing["lease_expires_at"]) + 1)
    resolved = bridge.edge_reconcile(request, token=enrolled["node_token"])
    repeated = bridge.edge_reconcile(request, token=enrolled["node_token"])

    saved_operation = store.get_operation(operation["operation_id"])
    saved_attempt = store.get_attempt(executing["attempt_id"])
    assert waiting["accepted"] is False
    assert waiting["reason"] == "reconciliation_waiting_for_lease_expiry"
    assert "disposition" not in waiting
    assert resolved["disposition"] == "manual_recovery"
    assert repeated["disposition"] == "manual_recovery"
    assert saved_attempt["state"] == "manual_recovery"
    assert saved_operation["state"] == "blocked"
    blocker = saved_operation["result"]["result"]
    assert blocker["reason"] == EDGE_OUTCOME_UNKNOWN_REASON
    assert "patchbay_operation_status" in blocker["manager_guidance"]
    assert "retry_attempts" not in resolved
    assert "resume_attempts" not in resolved
    store.close()


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
