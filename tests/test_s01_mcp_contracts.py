from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from patchbay.hub.operations import public_envelope
from patchbay.hub.protocol import HubProtocol
from patchbay.hub.protocol_v2 import HubProtocolV2
from patchbay.hub.protocol_v2_legacy import validate_hub_v2_tool_arguments
from patchbay.hub.server_v2 import create_hub_v2_server
from patchbay.hub.store_v2 import HubStoreV2
from patchbay.mcp_versions import SUPPORTED_MCP_VERSIONS, select_mcp_version
from patchbay.protocol.context import RequestContext
from patchbay.protocol.mcp import MCPProtocol
from patchbay.protocol.result_text import FIELDS
from test_hub_server_v2 import StatefulHubV2App


class Handler:
    async def handle_tool_call(self, name, arguments, *, context=None):
        return public_envelope("ok")


def rpc(protocol, method, params, context=None):
    return asyncio.run(protocol.handle_message({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, context=context))


@pytest.mark.parametrize("kind", ["local", "hub-v2", "hub-v1"])
def test_manager_public_entrypoints_negotiate_only_supported_versions(kind):
    p = MCPProtocol({}, Handler()) if kind == "local" else HubProtocolV2(Handler()) if kind == "hub-v2" else HubProtocol(None)
    for requested in (*SUPPORTED_MCP_VERSIONS, "2099-99-99"):
        session = {}
        response = rpc(p, "initialize", {"protocolVersion": requested}, RequestContext(session_data=session))
        version = response["result"]["protocolVersion"]
        assert version == (requested if requested in SUPPORTED_MCP_VERSIONS else "2025-11-25")
        assert session["protocol_version"] == version
        assert "tasks" not in response["result"]["capabilities"]
    for invalid in (None, "", " ", True, 12, [], {}):
        assert rpc(p, "initialize", {"protocolVersion": invalid})["error"]["code"] == -32602
        with pytest.raises(ValueError):
            select_mcp_version(invalid)


@pytest.mark.parametrize("surface", ["local", "hub"])
def test_manager_http_negotiation_and_subsequent_header_are_consistent(surface, monkeypatch):
    if surface == "hub":
        app = create_hub_v2_server({"auth": {"enabled": False}}, hub_app=StatefulHubV2App())
    else:
        from patchbay import server
        monkeypatch.setattr(server, "sessions", {})
        monkeypatch.setattr(server, "work_runs", {})
        monkeypatch.setattr(server, "mcp_protocol", MCPProtocol({}, Handler()))
        monkeypatch.setattr(server.evidence_recorder, "record_mcp_event", lambda **kwargs: None)
        app = server.app
    client = TestClient(app)
    init = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": "2099-99-99"}})
    assert init.status_code == 200
    assert init.json()["result"]["protocolVersion"] == "2025-11-25"
    headers = {"Mcp-Session-Id": init.headers["Mcp-Session-Id"]}
    message = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    for version in ("2099-99-99", "2025-03-26", ""):
        response = client.post("/mcp", json=message, headers={**headers, "MCP-Protocol-Version": version})
        assert response.status_code == 400 and response.json()["error"]["code"] == -32602
    for extra in ({}, {"MCP-Protocol-Version": "2025-11-25"}):
        assert client.post("/mcp", json=message, headers={**headers, **extra}).status_code == 200


@pytest.mark.parametrize("tool,left,right,needle", [
    ("codex_worker_message", {"accepted": True}, {"accepted": False, "reason": "cleanup_pending"}, '"accepted":false'),
    ("codex_worker_integrate", {"applied": True}, {"applied": False, "reason": "stale_preview"}, '"applied":false'),
    ("codex_worker_inspect", {"truncated": False}, {"truncated": True, "next_start_line": 2}, '"next_start_line":2'),
    ("codex_worker_status", {"changed": True}, {"changed": False, "status_current": False}, '"status_current":false'),
    ("codex_worker_stop", {"stopped": True}, {"stopped": False, "cleanup_unresolved": True}, '"cleanup_unresolved":true'),
])
def test_text_only_decisions_distinguish_outcomes_and_full_structured_evidence_is_untouched(tool, left, right, needle):
    p = MCPProtocol({}, Handler())
    base = {"name": "Luna", "worker_id": "worker_fixture", "state": "completed", "note": "λ" * 30000,
            "text": "first\n" + "長" * 10000 + "\nlast failure", "sha256": "a" * 64}
    a, b = {**base, **left}, {**base, **right}
    before = deepcopy(b)
    text_a, text_b = p._worker_tool_result_text(a, tool), p._worker_tool_result_text(b, tool)
    assert text_a != text_b and needle in text_b and "worker_fixture" in text_b
    assert b == before and b["text"].endswith("last failure")
    assert len(text_b.encode("utf-8")) < 12000


def test_stop_hint_does_not_grant_force_or_discard_authority():
    text = MCPProtocol({}, Handler())._worker_tool_result_text({"name": "Luna", "stop_confirmation_required": True}, "codex_worker_stop")
    assert "force=true" not in text
    assert "Force requires authority" in text and "stop does not authorize discarding files" in text


def test_hub_text_preserves_operation_state_page_source_and_callable_continuation():
    result = {"summary": "Same summary", "worker_id": "worker_fixture", "applied": False,
              "reason": "stale_preview", "path": "app.py", "sha256": "b" * 64,
              "partial": True, "next_start_line": 2, "truncated": True}
    next_action = {"tool": "patchbay_operation_status", "arguments": {"operation_id": "op_fixture"}}
    output = public_envelope("pending", result=result, operation={"operation_id": "op_fixture", "state": "outcome_unknown"}, next_actions=[next_action])
    text = HubProtocolV2._compact_text("patchbay_worker_inspect", output)
    assert '"applied":false' in text and '"next_start_line":2' in text
    assert "outcome_unknown" in text and json.dumps(next_action, separators=(",", ":")) in text
    assert len(text) < 2500
    oversized = {key: "x" * 100000 for key in FIELDS}
    assert len(MCPProtocol({}, Handler())._worker_tool_result_text(oversized, "codex_worker_message")) < 12000
    output["operation"]["operation_id"] = "x" * 100000
    text = HubProtocolV2._compact_text("patchbay_worker_inspect", output)
    assert len(text) < 2500 and '"summary_omitted_fields":["operation_id"]' in text


def test_invalid_hub_output_cannot_supply_an_unverified_operation_identity():
    class BadHandler:
        async def handle_tool_call(self, *args, **kwargs):
            return {"operation": {"operation_id": "op_" + "a" * 32}, "private": "untrusted-result"}
    reply = rpc(HubProtocolV2(BadHandler()), "tools/call", {"name": "patchbay_work_group_create", "arguments": {
        "title": "Fixture", "goal": "No invented identity", "idempotency_key": "original"}})
    data = reply["error"]["data"]
    assert data["effect_may_exist"] is True and "operation_id" not in data
    assert "op_" + "a" * 32 not in json.dumps(reply)
    assert data["recovery_actions"] == [{"tool": "patchbay_work_group_list", "arguments": {}}]


def test_hub_post_admission_output_fault_has_existing_receipt_and_exact_retry(tmp_path):
    store = HubStoreV2(tmp_path / "hub.sqlite3")
    class DurableHandler:
        corrupt = True
        def __init__(self):
            self.store = store
        async def handle_tool_call(self, name, arguments, *, context=None):
            if name == "patchbay_operation_status":
                operation = store.get_operation(arguments["operation_id"])
            else:
                operation = store.create_operation(tool=name, logical_target="fixture", idempotency_key=arguments["idempotency_key"], payload=arguments)
            result = public_envelope("pending", operation={"operation_id": operation["operation_id"], "state": operation["state"]})
            if self.corrupt and name != "patchbay_operation_status":
                result["status"] = "malformed-output"
                result["private"] = "never-return-this"
            return result
    handler = DurableHandler()
    p = HubProtocolV2(handler)
    args = {"title": "Fixture", "goal": "One durable admission", "idempotency_key": "original-key"}
    first = rpc(p, "tools/call", {"name": "patchbay_work_group_create", "arguments": args})
    assert "result" not in first and first["error"]["code"] == -32603
    data = first["error"]["data"]
    assert data["boundary"] == "effect_may_exist_result_invalid" and data["original_key"] == "original-key"
    assert "never-return-this" not in json.dumps(first)
    for action in data["recovery_actions"]:
        validate_hub_v2_tool_arguments(action["tool"], action["arguments"])
        assert "error" not in rpc(p, "tools/call", {"name": action["tool"], "arguments": action["arguments"]})
    handler.corrupt = False
    replay = rpc(p, "tools/call", {"name": "patchbay_work_group_create", "arguments": args})
    assert replay["result"]["structuredContent"]["operation"]["operation_id"] == data["operation_id"]
    # Drop one successful response entirely, then retry unchanged.
    rpc(p, "tools/call", {"name": "patchbay_work_group_create", "arguments": args})
    assert store._connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
    store.close()
