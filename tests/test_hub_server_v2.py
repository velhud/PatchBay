from __future__ import annotations

import asyncio
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import httpx
import pytest
from fastapi.testclient import TestClient

from patchbay.hub.broker import OperationBroker
from patchbay.hub.backup_v2 import (
    AdmissionFreezeController,
    admission_coordination_path,
)
from patchbay.hub.edge import build_capabilities
from patchbay.hub.edge_client_v2 import edge_contract_metadata
from patchbay.hub.edge_journal import EdgeJournal
from patchbay.hub.edge_v2 import EdgeExecutionService
from patchbay.hub.operations import public_envelope
from patchbay.hub.runtime_v2 import HubRuntimeV2
from patchbay.hub.server_v2 import (
    DEFAULT_MAX_MCP_SESSIONS,
    DEFAULT_MCP_SESSION_TTL_SECONDS,
    create_hub_v2_server,
)
from patchbay.hub.store_v2 import HubStoreV2, HubStoreV2Conflict
from patchbay.hub.tool_surface import (
    HUB_V2_CONTRACT_HASH,
    HUB_V2_EXPECTED_TOOL_COUNT,
    HUB_V2_TOOL_NAMES,
)
from patchbay.hub.transport_v2 import (
    HubPullTransportBridgeV2,
    edge_reconciliation_requests,
)
from patchbay.protocol.context import RequestContext


OPERATOR_TOKEN = "operator-http-token"
EDGE_CONTRACT = "contract-v2-test"


class FakeMonotonicClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class StatefulHubV2App:
    """Small injected application used to verify the server's HTTP contract."""

    principal_ref = "principal_test_operator"

    def __init__(self) -> None:
        self.enrollment_codes = {"PB-ONE", "PB-TWO"}
        self.generation_number = 0
        self.machine: dict[str, Any] = {}
        self.node_token = ""
        self.projection_revision = 0
        self.projection: dict[str, Any] = {}
        self.contexts: list[RequestContext] = []
        self.attempt: dict[str, Any] | None = None
        self.terminal_result: dict[str, Any] | None = None
        self.acknowledged_receipts: list[str] = []

    async def handle_tool_call(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        context: RequestContext | None = None,
    ) -> Mapping[str, Any]:
        assert context is not None
        self.contexts.append(context)
        return public_envelope(
            "ok",
            result={
                "summary": f"handled {name}",
                "arguments_seen": dict(arguments),
                "api_key": "must-not-cross-mcp",
                "node_token": "must-not-cross-mcp",
                "payload": {"brief": "must-not-cross-mcp"},
                "worker_payload": {"message": "must-not-cross-mcp"},
            },
        )

    def edge_enroll(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        code = str(payload["code"])
        if code not in self.enrollment_codes:
            raise ValueError("Unknown enrollment code")
        self.enrollment_codes.remove(code)
        self.generation_number += 1
        generation = f"edgegen_test_{self.generation_number}"
        self.node_token = f"node-token-{self.generation_number}"
        self.machine = {
            "machine_id": str(payload["machine_id"]),
            "edge_generation": generation,
            "display_name": str(payload.get("display_name") or payload["machine_id"]),
        }
        self.projection_revision = 0
        self.projection = {}
        return {
            "machine": deepcopy(self.machine),
            "edge_generation": generation,
            "node_token": self.node_token,
        }

    def edge_heartbeat(
        self,
        payload: Mapping[str, Any],
        *,
        token: str,
    ) -> Mapping[str, Any]:
        self._authenticate(payload, token)
        revision = int(payload["projection_revision"])
        accepted = revision > self.projection_revision
        if accepted:
            self.projection_revision = revision
        return {
            "accepted": True,
            "projection_accepted": accepted,
            "current_projection_revision": self.projection_revision,
            "machine": deepcopy(self.machine),
        }

    def edge_claim(
        self,
        payload: Mapping[str, Any],
        *,
        token: str,
    ) -> Mapping[str, Any]:
        self._authenticate(payload, token)
        if payload["contract_hash"] != EDGE_CONTRACT:
            raise HubStoreV2Conflict("attempt_contract_hash_mismatch")
        if self.attempt is None:
            return {"attempt": None}
        self._attempt_identity(payload)
        if self.attempt["state"] == "offered":
            self.attempt["state"] = "claimed"
            self.attempt["revision"] += 1
        return {"attempt": deepcopy(self.attempt)}

    def edge_lease(
        self,
        payload: Mapping[str, Any],
        *,
        token: str,
    ) -> Mapping[str, Any]:
        self._authenticate(payload, token)
        self._attempt_identity(payload)
        assert self.attempt is not None
        if int(payload["expected_revision"]) != self.attempt["revision"]:
            raise HubStoreV2Conflict("stale_attempt_revision")
        self.attempt["revision"] += 1
        self.attempt["lease_expires_at"] = 999.0
        return {"attempt": deepcopy(self.attempt)}

    def edge_result(
        self,
        payload: Mapping[str, Any],
        *,
        token: str,
    ) -> Mapping[str, Any]:
        self._authenticate(payload, token)
        receipt = payload.get("receipt")
        if not isinstance(receipt, Mapping):
            raise ValueError("receipt is required")
        self._attempt_identity(receipt)
        assert self.attempt is not None
        if self.terminal_result is not None:
            if self.terminal_result != receipt.get("result"):
                raise HubStoreV2Conflict("conflicting_terminal_receipt")
            return {
                "accepted": True,
                "duplicate": True,
                "receipt_acknowledgements": [receipt["receipt_id"]],
            }
        self.terminal_result = deepcopy(dict(receipt.get("result") or {}))
        self.attempt["state"] = "result_ready"
        self.attempt["revision"] += 1
        return {
            "accepted": True,
            "duplicate": False,
            "receipt_acknowledgements": [receipt["receipt_id"]],
        }

    def edge_outbox_ack(
        self,
        payload: Mapping[str, Any],
        *,
        token: str,
    ) -> Mapping[str, Any]:
        self._authenticate(payload, token)
        receipts = payload.get("receipt_ids")
        if not isinstance(receipts, list):
            raise ValueError("receipt_ids is required")
        self.acknowledged_receipts.extend(str(item) for item in receipts)
        return {"acknowledged_receipts": list(self.acknowledged_receipts)}

    def edge_projection(
        self,
        payload: Mapping[str, Any],
        *,
        token: str,
    ) -> Mapping[str, Any]:
        self._authenticate(payload, token)
        revision = int(payload["projection_revision"])
        if revision <= self.projection_revision:
            return {
                "accepted": True,
                "projection_accepted": False,
                "current_projection_revision": self.projection_revision,
            }
        gap = self.projection_revision > 0 and revision > self.projection_revision + 1
        projection = payload.get("projection")
        projection = dict(projection) if isinstance(projection, Mapping) else {}
        if gap and projection.get("snapshot_kind") != "full":
            return {
                "accepted": True,
                "projection_accepted": False,
                "request_full_snapshot": True,
                "current_projection_revision": self.projection_revision,
            }
        self.projection_revision = revision
        self.projection = deepcopy(projection)
        return {
            "accepted": True,
            "projection_accepted": True,
            "request_full_snapshot": False,
            "current_projection_revision": self.projection_revision,
        }

    def edge_reconcile(
        self,
        payload: Mapping[str, Any],
        *,
        token: str,
    ) -> Mapping[str, Any]:
        self._authenticate(payload, token)
        self._attempt_identity(payload)
        assert self.attempt is not None
        return {
            "found": True,
            "attempt": deepcopy(self.attempt),
            "terminal_result": deepcopy(self.terminal_result or {}),
        }

    def offer_attempt(self) -> dict[str, Any]:
        if not self.machine:
            raise AssertionError("enroll before offering an attempt")
        self.attempt = {
            "operation_id": "op_test_1",
            "attempt_id": "attempt_test_1",
            "machine_id": self.machine["machine_id"],
            "edge_generation": self.machine["edge_generation"],
            "contract_hash": EDGE_CONTRACT,
            "fencing_token": 7,
            "revision": 1,
            "state": "offered",
            "payload": {"brief": "private edge payload"},
        }
        return deepcopy(self.attempt)

    def _authenticate(self, payload: Mapping[str, Any], token: str) -> None:
        if token != self.node_token:
            raise ValueError("Unauthorized edge node")
        if payload.get("machine_id") != self.machine.get("machine_id"):
            raise ValueError("Unauthorized edge node")
        if payload.get("edge_generation") != self.machine.get("edge_generation"):
            raise ValueError("Edge generation is not current for this machine")

    def _attempt_identity(self, payload: Mapping[str, Any]) -> None:
        if self.attempt is None:
            raise KeyError("attempt")
        expected = {
            "operation_id": self.attempt["operation_id"],
            "attempt_id": self.attempt["attempt_id"],
            "edge_generation": self.attempt["edge_generation"],
            "contract_hash": self.attempt["contract_hash"],
            "fencing_token": self.attempt["fencing_token"],
        }
        for field, value in expected.items():
            if payload.get(field) != value:
                raise HubStoreV2Conflict(f"attempt_{field}_mismatch")


class RuntimeBackedHubV2App:
    def __init__(self, path: Path) -> None:
        self.store = HubStoreV2(path)
        self.broker = OperationBroker(self.store)
        self.runtime = HubRuntimeV2(self.store, broker=self.broker)

    async def handle_tool_call(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        context: RequestContext | None = None,
    ) -> Mapping[str, Any]:
        return await self.runtime.handle_tool_call(name, arguments, context=context)

    def close(self) -> None:
        self.store.close()


def _online_runtime_edge(
    app: RuntimeBackedHubV2App,
    *,
    contract_hash: str = HUB_V2_CONTRACT_HASH,
    action_version: str = "2",
) -> tuple[dict[str, Any], dict[str, Any], HubPullTransportBridgeV2]:
    code = app.runtime.create_enrollment_code(name="Runtime Edge", tags=["codex"])[
        "code"
    ]
    enrolled = app.runtime.enroll_machine(
        code=code,
        machine_id="runtime-machine",
        edge_generation="runtime-generation",
        display_name="Runtime Edge",
        tags=["codex"],
    )
    capabilities = {
        "contract_hash": contract_hash,
        "action_capabilities": {
            "codex_worker_inbox": action_version,
            "codex_worker_start": action_version,
            "codex_worker_stop": action_version,
            "codex_open_workspace": action_version,
        },
        "action_capability_versions": {
            "codex_worker_inbox": action_version,
            "codex_worker_start": action_version,
            "codex_worker_stop": action_version,
            "codex_open_workspace": action_version,
        },
        "max_concurrent_jobs": 2,
        "queue_enabled": True,
    }
    app.runtime.heartbeat(
        machine_id="runtime-machine",
        token=enrolled["node_token"],
        edge_generation="runtime-generation",
        projection_revision=1,
        capabilities=capabilities,
        workspaces=[],
        resource_status={"active_workers": 0, "free_worker_slots": 2},
    )
    return enrolled, capabilities, HubPullTransportBridgeV2(app)


def _runtime_dispatch(
    app: RuntimeBackedHubV2App,
    transport: HubPullTransportBridgeV2,
    *,
    operation_key: str,
    action: str = "codex_worker_stop",
    tool: str = "patchbay_worker_stop",
    arguments: Mapping[str, Any] | None = None,
    payload_fields: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    payload = {
        "action": action,
        "arguments": dict(arguments or {"worker": operation_key}),
        "machine_id": "runtime-machine",
        "edge_generation": "runtime-generation",
        "target": {
            "machine_id": "runtime-machine",
            "edge_generation": "runtime-generation",
        },
    }
    payload.update(deepcopy(dict(payload_fields or {})))
    operation = app.broker.create_operation(
        tool=tool,
        logical_target=operation_key,
        idempotency_key=operation_key,
        payload=payload,
    )
    operation = app.broker.prepare_operation(
        operation["operation_id"], expected_revision=int(operation["revision"])
    )
    assert operation is not None
    operation = app.broker.make_dispatchable(
        operation["operation_id"], expected_revision=int(operation["revision"])
    )
    assert operation is not None
    dispatch = transport._persist_dispatch(operation, payload)
    attempt = transport._offer_dispatch(operation, dispatch)
    return operation, dispatch, attempt


def _server(
    app: StatefulHubV2App,
    *,
    max_request_bytes: int = 4096,
    inject_factory: bool = False,
):
    dependency = (
        {"hub_app_factory": lambda: app} if inject_factory else {"hub_app": app}
    )
    return create_hub_v2_server(
        {
            "server": {"host": "0.0.0.0", "max_request_bytes": max_request_bytes},
            "auth": {"enabled": True},
        },
        environ={"PATCHBAY_HTTP_TOKEN": OPERATOR_TOKEN},
        **dependency,
    )


def test_runtime_controller_is_the_production_pull_transport(tmp_path: Path) -> None:
    app = RuntimeBackedHubV2App(tmp_path / "runtime-controller.sqlite3")
    server = create_hub_v2_server({"auth": {"enabled": False}}, hub_app=app)
    controller = server.state.hub_v2_edge_app

    assert isinstance(controller, HubPullTransportBridgeV2)
    assert type(controller).__bases__ == (HubPullTransportBridgeV2,)
    for method_name in (
        "edge_heartbeat",
        "edge_projection",
        "edge_claim",
        "edge_lease",
        "edge_result",
        "edge_outbox_ack",
        "edge_reconcile",
        "_hydrate_transient_payload",
        "_acknowledge_transient_payload",
        "_record_group_preflight_if_needed",
        "_record_group_preflight_invalidation_if_needed",
        "_record_receipt",
    ):
        assert getattr(type(controller), method_name) is getattr(
            HubPullTransportBridgeV2, method_name
        )
    app.close()


def test_preflight_normalization_preserves_explicit_read_only_failure(
    tmp_path: Path,
) -> None:
    app = RuntimeBackedHubV2App(tmp_path / "preflight-failure.sqlite3")
    _, _, transport = _online_runtime_edge(app)

    normalized = transport._normalize_preflight_result(
        {
            "accepted": False,
            "failed": True,
            "reason": "read_only_handler_failed",
            "repo_requested": "/workspace/missing",
        },
        {
            "machine_id": "runtime-machine",
            "payload": {"repo_path": "/workspace/missing"},
        },
    )

    assert normalized["ok"] is False
    assert normalized["repo_exists"] is False
    assert normalized["repo_requested"] == "/workspace/missing"
    assert normalized["repo_resolved"] == "/workspace/missing"
    app.close()


def _operator_headers(**extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {OPERATOR_TOKEN}", **extra}


def _edge_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _enroll(client: TestClient, code: str = "PB-ONE") -> dict[str, Any]:
    response = client.post(
        "/edge/v2/enroll",
        json={"code": code, "machine_id": "machine_test", "display_name": "Test Edge"},
    )
    assert response.status_code == 200
    return response.json()


def test_operator_auth_request_limit_and_exact_31_tool_surface() -> None:
    app = StatefulHubV2App()
    client = TestClient(_server(app, max_request_bytes=768, inject_factory=True))

    assert client.get("/status").status_code == 401
    assert (
        client.get("/status", headers=_operator_headers()).json()["principal_ref"]
        == app.principal_ref
    )
    assert (
        client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        ).status_code
        == 401
    )

    listed = client.post(
        "/mcp",
        headers=_operator_headers(),
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert listed.status_code == 200
    assert listed.headers["Mcp-Session-Id"]
    names = tuple(tool["name"] for tool in listed.json()["result"]["tools"])
    assert names == HUB_V2_TOOL_NAMES
    assert len(names) == HUB_V2_EXPECTED_TOOL_COUNT == 31

    too_large = client.post(
        "/mcp",
        headers=_operator_headers(),
        content=b"{" + (b'"padding":"' + b"x" * 800 + b'"}'),
    )
    assert too_large.status_code == 413
    assert too_large.json()["error"]["code"] == "request_too_large"
    edge_too_large = client.post(
        "/edge/v2/enroll",
        content=b"{" + (b'"padding":"' + b"x" * 800 + b'"}'),
    )
    assert edge_too_large.status_code == 413


def test_mcp_sessions_expire_by_monotonic_ttl_and_reinitialize_without_identity_reuse() -> (
    None
):
    clock = FakeMonotonicClock(100.0)
    app = StatefulHubV2App()
    server = create_hub_v2_server(
        {"auth": {"enabled": False}},
        hub_app=app,
        monotonic_clock=clock,
    )
    client = TestClient(server)
    initialized = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "ttl-client", "version": "1"}},
        },
    )
    session_id = initialized.headers["Mcp-Session-Id"]
    called = client.post(
        "/mcp",
        headers={"Mcp-Session-Id": session_id},
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "patchbay_work_group_status",
                "arguments": {"work_group_id": "old-group"},
                "_meta": {"openai/session": "old-chat"},
            },
        },
    )
    assert called.status_code == 200
    old_client_ref = server.state.hub_v2_sessions[session_id]["client_ref"]

    clock.advance(DEFAULT_MCP_SESSION_TTL_SECONDS)
    expired = client.post(
        "/mcp",
        headers={"Mcp-Session-Id": session_id},
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
    )
    assert expired.status_code == 404
    assert expired.json()["error"] == {
        "code": -32001,
        "message": "Unknown or expired MCP session",
    }

    reinitialized = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "initialize",
            "params": {"clientInfo": {"name": "new-client", "version": "1"}},
        },
    )
    new_session_id = reinitialized.headers["Mcp-Session-Id"]
    new_session = server.state.hub_v2_sessions[new_session_id]
    assert new_session_id != session_id
    assert new_session["client_ref"] != old_client_ref
    assert "chatgpt_session_ref" not in new_session
    assert "work_group_id" not in new_session
    assert len(server.state.hub_v2_sessions) == 1


def test_mcp_session_lru_bounds_three_thousand_abandoned_clients() -> None:
    app = StatefulHubV2App()
    server = create_hub_v2_server({"auth": {"enabled": False}}, hub_app=app)
    client = TestClient(server)
    session_ids = []
    for request_id in range(3_000):
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": f"client-{request_id}", "version": "1"}
                },
            },
        )
        assert response.status_code == 200
        session_ids.append(response.headers["Mcp-Session-Id"])

    assert len(server.state.hub_v2_sessions) == DEFAULT_MAX_MCP_SESSIONS
    assert session_ids[0] not in server.state.hub_v2_sessions
    assert session_ids[-1] in server.state.hub_v2_sessions
    assert (
        client.get("/status").json()["active_mcp_sessions"] == DEFAULT_MAX_MCP_SESSIONS
    )
    assert (
        client.post(
            "/mcp",
            headers={"Mcp-Session-Id": session_ids[0]},
            json={"jsonrpc": "2.0", "id": 3_001, "method": "tools/list", "params": {}},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/mcp",
            headers={"Mcp-Session-Id": session_ids[-1]},
            json={"jsonrpc": "2.0", "id": 3_002, "method": "tools/list", "params": {}},
        ).status_code
        == 200
    )


@pytest.mark.asyncio
async def test_mcp_session_capacity_never_evicts_an_in_flight_identity() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingHubV2App(StatefulHubV2App):
        async def handle_tool_call(
            self,
            name: str,
            arguments: Mapping[str, Any],
            *,
            context: RequestContext | None = None,
        ) -> Mapping[str, Any]:
            if name == "patchbay_work_group_status":
                entered.set()
                await release.wait()
            return await super().handle_tool_call(name, arguments, context=context)

    app = BlockingHubV2App()
    server = create_hub_v2_server(
        {"auth": {"enabled": False}},
        hub_app=app,
        max_mcp_sessions=1,
    )
    transport = httpx.ASGITransport(app=server)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        first_session = first.headers["Mcp-Session-Id"]
        blocked_request = asyncio.create_task(
            client.post(
                "/mcp",
                headers={"Mcp-Session-Id": first_session},
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "patchbay_work_group_status",
                        "arguments": {"work_group_id": "active-group"},
                    },
                },
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=2)

        second = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {}},
        )
        second_session = second.headers["Mcp-Session-Id"]
        assert first_session in server.state.hub_v2_sessions
        assert server.state.hub_v2_in_flight_sessions[first_session] == 1
        assert len(server.state.hub_v2_sessions) == 2

        release.set()
        assert (await blocked_request).status_code == 200
        assert len(server.state.hub_v2_sessions) == 1
        assert second_session in server.state.hub_v2_sessions
        assert first_session not in server.state.hub_v2_sessions
        assert not server.state.hub_v2_in_flight_sessions
        assert (
            await client.post(
                "/mcp",
                headers={"Mcp-Session-Id": second_session},
                json={"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
            )
        ).status_code == 200


def test_enrollment_rotates_generation_and_old_node_credentials_stop_working() -> None:
    app = StatefulHubV2App()
    client = TestClient(_server(app))

    first = _enroll(client)
    first_generation = first["edge_generation"]
    first_token = first["node_token"]
    heartbeat = {
        "machine_id": "machine_test",
        "edge_generation": first_generation,
        "projection_revision": 1,
    }
    assert (
        client.post(
            "/edge/v2/heartbeat", headers=_edge_headers(first_token), json=heartbeat
        ).status_code
        == 200
    )

    second = _enroll(client, "PB-TWO")
    assert second["edge_generation"] != first_generation
    assert second["node_token"] != first_token

    stale = client.post(
        "/edge/v2/heartbeat", headers=_edge_headers(first_token), json=heartbeat
    )
    assert stale.status_code == 401
    current = client.post(
        "/edge/v2/heartbeat",
        headers=_edge_headers(second["node_token"]),
        json={
            **heartbeat,
            "edge_generation": second["edge_generation"],
            "projection_revision": 1,
        },
    )
    assert current.status_code == 200
    assert current.json()["projection_accepted"] is True


def test_claim_lease_result_outbox_ack_and_stale_attempt_fence() -> None:
    app = StatefulHubV2App()
    client = TestClient(_server(app))
    enrolled = _enroll(client)
    offered = app.offer_attempt()
    identity = {
        "machine_id": "machine_test",
        "edge_generation": enrolled["edge_generation"],
        "contract_hash": EDGE_CONTRACT,
        "operation_id": offered["operation_id"],
        "attempt_id": offered["attempt_id"],
        "fencing_token": offered["fencing_token"],
    }
    headers = _edge_headers(enrolled["node_token"])

    claimed = client.post("/edge/v2/claim", headers=headers, json=identity)
    assert claimed.status_code == 200
    assert claimed.json()["attempt"]["state"] == "claimed"
    assert claimed.json()["attempt"]["payload"]["brief"] == "private edge payload"

    leased = client.post(
        "/edge/v2/lease",
        headers=headers,
        json={**identity, "expected_revision": claimed.json()["attempt"]["revision"]},
    )
    assert leased.status_code == 200
    assert leased.json()["attempt"]["lease_expires_at"] == 999.0

    receipt = {
        **identity,
        "receipt_id": "receipt_test_1",
        "result": {"status": "ok", "worker_id": "worker_test"},
    }
    finished = client.post(
        "/edge/v2/result",
        headers=headers,
        json={
            "machine_id": "machine_test",
            "edge_generation": enrolled["edge_generation"],
            "receipt": receipt,
        },
    )
    assert finished.status_code == 200
    assert finished.json()["receipt_acknowledgements"] == ["receipt_test_1"]

    acknowledged = client.post(
        "/edge/v2/outbox/ack",
        headers=headers,
        json={
            "machine_id": "machine_test",
            "edge_generation": enrolled["edge_generation"],
            "receipt_ids": ["receipt_test_1"],
        },
    )
    assert acknowledged.status_code == 200
    assert acknowledged.json()["acknowledged_receipts"] == ["receipt_test_1"]

    stale_receipt = {
        **receipt,
        "attempt_id": "attempt_stale",
        "result": {"status": "failed"},
    }
    stale = client.post(
        "/edge/v2/result",
        headers=headers,
        json={
            "machine_id": "machine_test",
            "edge_generation": enrolled["edge_generation"],
            "receipt": stale_receipt,
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "attempt_attempt_id_mismatch"
    assert app.terminal_result == {"status": "ok", "worker_id": "worker_test"}


def test_projection_revision_reconciliation_and_request_context_are_preserved() -> None:
    app = StatefulHubV2App()
    client = TestClient(_server(app))
    enrolled = _enroll(client)
    app.offer_attempt()
    headers = _edge_headers(enrolled["node_token"])
    edge_identity = {
        "machine_id": "machine_test",
        "edge_generation": enrolled["edge_generation"],
    }

    first = client.post(
        "/edge/v2/projection",
        headers=headers,
        json={
            **edge_identity,
            "projection_revision": 1,
            "projection": {"snapshot_kind": "full", "workers": []},
        },
    )
    assert first.status_code == 200
    assert first.json()["projection_accepted"] is True
    duplicate = client.post(
        "/edge/v2/projection",
        headers=headers,
        json={
            **edge_identity,
            "projection_revision": 1,
            "projection": {
                "snapshot_kind": "delta",
                "workers": [{"worker_id": "wrong"}],
            },
        },
    )
    assert duplicate.json()["projection_accepted"] is False
    assert app.projection == {"snapshot_kind": "full", "workers": []}

    reconcile = client.post(
        "/edge/v2/reconcile",
        headers=headers,
        json={
            **edge_identity,
            "operation_id": "op_test_1",
            "attempt_id": "attempt_test_1",
            "contract_hash": EDGE_CONTRACT,
            "fencing_token": 7,
        },
    )
    assert reconcile.status_code == 200
    assert reconcile.json()["found"] is True

    called = client.post(
        "/mcp",
        headers=_operator_headers(),
        json={
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                "name": "patchbay_work_group_status",
                "arguments": {"work_group_id": "group_test"},
                "_meta": {
                    "openai/session": "raw-session-secret",
                    "openai/subject": "raw-subject-secret",
                },
            },
        },
    )
    assert called.status_code == 200
    structured = called.json()["result"]["structuredContent"]
    assert structured["result"]["summary"] == "handled patchbay_work_group_status"
    assert "api_key" not in structured["result"]
    assert "arguments_seen" not in structured["result"]
    assert "node_token" not in structured["result"]
    assert "payload" not in structured["result"]
    assert "worker_payload" not in structured["result"]

    context = app.contexts[-1]
    assert context.owner_ref == app.principal_ref
    assert context.owner_scope == "server"
    assert context.client_ref.startswith("client_")
    assert context.chatgpt_session_ref.startswith("chatgpt_session_")
    assert context.chatgpt_subject_ref.startswith("chatgpt_subject_")
    assert context.work_run_ref.startswith("run_")
    assert context.work_group_id == "group_test"
    assert context.tool_mode == "hub-v2"
    assert context.active_mcp_sessions == 1
    assert context.transport_session_id is not None
    assert context.transport_session_id not in called.text


def test_runtime_backed_edge_claim_result_and_stale_attempt_are_fenced(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "hub-v2.sqlite3"
    app = RuntimeBackedHubV2App(state_path)
    principal_ref = app.store.principal_ref
    enrollment = app.runtime.create_enrollment_code(name="Runtime Edge")
    server = _server(app)
    controller = server.state.hub_v2_edge_app
    assert isinstance(controller, HubPullTransportBridgeV2)
    client = TestClient(server)
    capabilities = build_capabilities({})
    enrolled_response = client.post(
        "/edge/v2/enroll",
        json={
            "code": enrollment["code"],
            "machine_id": "machine_runtime",
            "display_name": "Runtime Edge",
            "capabilities": capabilities,
            "workspaces": [],
        },
    )
    assert enrolled_response.status_code == 200
    enrolled = enrolled_response.json()
    generation = enrolled["edge_generation"]
    token = enrolled["node_token"]
    contract = edge_contract_metadata(capabilities, edge_generation=generation)
    projected = client.post(
        "/edge/v2/projection",
        headers=_edge_headers(token),
        json={
            "machine_id": "machine_runtime",
            **contract,
            "projection_revision": 1,
            "projection": {"snapshot_kind": "full", "workers": [], "tombstones": []},
        },
    )
    assert projected.status_code == 200
    assert projected.json()["projection_accepted"] is True

    dispatch_payload = {
        "action": "codex_worker_start",
        "machine_id": "machine_runtime",
        "edge_generation": generation,
        "arguments": {
            "name": "Runtime Reader",
            "brief": "Inspect the runtime-backed server",
            "repo_path": "repo",
        },
        "target": {"machine_id": "machine_runtime", "edge_generation": generation},
        "context": {"owner_ref": app.store.principal_ref},
    }
    operation = app.broker.create_operation(
        tool="patchbay_worker_start",
        logical_target="Runtime Reader",
        idempotency_key="runtime-server-claim-1",
        payload=dispatch_payload,
    )
    operation = app.broker.prepare_operation(
        operation["operation_id"], expected_revision=operation["revision"]
    )
    assert operation is not None
    operation = app.broker.make_dispatchable(
        operation["operation_id"], expected_revision=operation["revision"]
    )
    assert operation is not None
    dispatch = controller._persist_dispatch(operation, dispatch_payload)
    controller._offer_dispatch(operation, dispatch)

    headers = _edge_headers(token)
    identity = {"machine_id": "machine_runtime", **contract}
    claimed = client.post(
        "/edge/v2/claim",
        headers=headers,
        json={**identity, "available_slots": 1, "max_attempts": 1},
    )
    assert claimed.status_code == 200
    attempt = claimed.json()["attempt"]
    assert attempt["edge_generation"] == generation
    assert attempt["required_contract_hash"] == capabilities["contract_hash"]
    assert attempt["action"] == "codex_worker_start"
    assert attempt["arguments"]["brief"] == "Inspect the runtime-backed server"
    journal = EdgeJournal(tmp_path / "edge-journal.sqlite3", edge_generation=generation)
    execution = EdgeExecutionService(
        object(),
        journal,
        machine_id="machine_runtime",
        capabilities=capabilities,
    )
    plan = execution.validate_attempt(attempt)
    journal.record_intent(
        operation_id=plan["operation_id"],
        attempt_id=plan["attempt_id"],
        fencing_token=plan["fencing_token"],
        action=plan["action"],
        target_key=plan["target_key"],
        payload=plan["payload"],
        payload_hash=plan["payload_hash"],
        edge_generation=generation,
    )
    journal.close()

    renewed = client.post(
        "/edge/v2/lease",
        headers=headers,
        json={
            **identity,
            "operation_id": attempt["operation_id"],
            "attempt_id": attempt["attempt_id"],
            "fencing_token": attempt["fencing_token"],
            "expected_revision": attempt["revision"],
            "lease_seconds": 30,
        },
    )
    assert renewed.status_code == 200
    assert renewed.json()["attempt"]["state"] == "executing"

    receipt = {
        "receipt_id": "receipt_runtime_1",
        "operation_id": attempt["operation_id"],
        "attempt_id": attempt["attempt_id"],
        "fencing_token": attempt["fencing_token"],
        "edge_generation": generation,
        "contract_hash": capabilities["contract_hash"],
        "operation_payload_hash": attempt["operation_payload_hash"],
        "result": {"accepted": True, "worker_id": "worker_runtime_1"},
    }
    finished = client.post(
        "/edge/v2/result",
        headers=headers,
        json={**identity, **receipt, "receipt": receipt},
    )
    assert finished.status_code == 200
    assert finished.json()["operation"]["state"] == "succeeded"
    assert finished.json()["attempt"]["state"] == "acknowledged"
    assert (
        finished.json()["receipt_acknowledgements"][0]["receipt_id"]
        == "receipt_runtime_1"
    )

    confirmed = client.post(
        "/edge/v2/outbox/ack",
        headers=headers,
        json={**identity, "receipt_ids": ["receipt_runtime_1"]},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["accepted"] is True

    stale = client.post(
        "/edge/v2/result",
        headers=headers,
        json={
            **identity,
            **receipt,
            "fencing_token": attempt["fencing_token"] + 1,
            "receipt": {**receipt, "fencing_token": attempt["fencing_token"] + 1},
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "attempt_fencing_token_mismatch"
    assert app.store.get_operation(attempt["operation_id"])["state"] == "succeeded"
    app.close()
    reopened = HubStoreV2(state_path)
    assert reopened.principal_ref == principal_ref
    reopened.close()


def test_uncertain_receipt_becomes_terminal_manual_recovery_blocker(
    tmp_path: Path,
) -> None:
    app = RuntimeBackedHubV2App(tmp_path / "uncertain-result.sqlite3")
    enrolled, capabilities, transport = _online_runtime_edge(app)
    operation, dispatch, _ = _runtime_dispatch(
        app, transport, operation_key="uncertain-result"
    )
    identity = {
        "machine_id": "runtime-machine",
        "edge_generation": "runtime-generation",
        "contract_hash": capabilities["contract_hash"],
    }
    claimed = transport.edge_claim(
        {**identity, "available_slots": 1, "max_attempts": 1, "lease_seconds": 30},
        token=enrolled["node_token"],
    )["attempt"]
    executing = transport.edge_lease(
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
    receipt = {
        "receipt_id": "receipt-uncertain-result",
        "operation_id": operation["operation_id"],
        "attempt_id": executing["attempt_id"],
        "fencing_token": executing["fencing_token"],
        "edge_generation": "runtime-generation",
        "contract_hash": capabilities["contract_hash"],
        "operation_payload_hash": dispatch["payload_hash"],
        "outcome": "outcome_unknown",
        "result": {"last_known_phase": "worker_start"},
        "error": "connection closed after effect boundary",
        "uncertain": True,
    }

    result = transport.edge_result(
        {**identity, "receipt": receipt}, token=enrolled["node_token"]
    )
    repeated = transport.edge_result(
        {**identity, "receipt": receipt}, token=enrolled["node_token"]
    )

    saved_operation = app.store.get_operation(operation["operation_id"])
    saved_attempt = app.store.get_attempt(executing["attempt_id"])
    saved_dispatch = app.store.get_entity(
        "hub.edge_dispatch", operation["operation_id"]
    )
    assert result["operation"]["state"] == "blocked"
    assert repeated["operation"]["state"] == "blocked"
    assert saved_operation["state"] == "blocked"
    assert saved_attempt["state"] == "manual_recovery"
    assert saved_dispatch["record"]["status"] == "blocked"
    assert saved_dispatch["record"]["blocker"]["reason"] == (
        "edge_outcome_unknown_requires_manual_recovery"
    )
    assert result["receipt_acknowledgements"][0]["receipt_id"] == receipt["receipt_id"]
    assert (
        edge_reconciliation_requests(
            app.store, "runtime-machine", int(saved_attempt["edge_generation"])
        )
        == []
    )
    app.close()


def test_exact_late_result_repairs_manual_recovery_without_weakening_fences(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "late-authoritative-result.sqlite3"
    app = RuntimeBackedHubV2App(state_path)
    enrolled, capabilities, transport = _online_runtime_edge(app)
    operation, dispatch, _ = _runtime_dispatch(
        app, transport, operation_key="late-authoritative-result"
    )
    identity = {
        "machine_id": "runtime-machine",
        "edge_generation": "runtime-generation",
        "contract_hash": capabilities["contract_hash"],
    }
    claimed = transport.edge_claim(
        {**identity, "available_slots": 1, "max_attempts": 1, "lease_seconds": 30},
        token=enrolled["node_token"],
    )["attempt"]
    executing = transport.edge_lease(
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
    app.broker.expire_leases(
        now=time.time() + 3_600,
        operation_id=operation["operation_id"],
    )
    blocked = transport.edge_reconcile(
        {
            **identity,
            "operation_id": operation["operation_id"],
            "attempt_id": executing["attempt_id"],
            "fencing_token": executing["fencing_token"],
            "local_recovery": {
                "found": True,
                "recovery_action": "reconcile_effect",
                "effect_started": True,
            },
        },
        token=enrolled["node_token"],
    )
    assert blocked["operation"]["state"] == "blocked"
    assert app.store.get_attempt(executing["attempt_id"])["state"] == "manual_recovery"

    receipt = {
        "receipt_id": "receipt-late-authoritative-result",
        "operation_id": operation["operation_id"],
        "attempt_id": executing["attempt_id"],
        "fencing_token": executing["fencing_token"],
        "edge_generation": "runtime-generation",
        "contract_hash": capabilities["contract_hash"],
        "operation_payload_hash": dispatch["payload_hash"],
        "outcome": "succeeded",
        "result": {"accepted": True, "worker_id": "worker-late-authoritative"},
    }
    repaired = transport.edge_result(
        {**identity, "receipt": receipt}, token=enrolled["node_token"]
    )
    replayed = transport.edge_result(
        {**identity, "receipt": receipt}, token=enrolled["node_token"]
    )

    assert repaired["operation"]["state"] == "succeeded"
    assert repaired["attempt"]["state"] == "acknowledged"
    assert replayed["operation"]["state"] == "succeeded"
    assert (
        app.store.get_entity("hub.edge_dispatch", operation["operation_id"])["record"][
            "status"
        ]
        == "complete"
    )
    with pytest.raises(HubStoreV2Conflict, match="attempt_fencing_token_mismatch"):
        transport.edge_result(
            {
                **identity,
                "fencing_token": executing["fencing_token"] + 1,
                "receipt": {
                    **receipt,
                    "fencing_token": executing["fencing_token"] + 1,
                    "result": {"accepted": True, "worker_id": "wrong-fence"},
                },
            },
            token=enrolled["node_token"],
        )
    assert app.store.get_operation(operation["operation_id"])["state"] == "succeeded"
    app.close()

    reopened = HubStoreV2(state_path)
    assert reopened.get_operation(operation["operation_id"])["state"] == "succeeded"
    assert reopened.get_attempt(executing["attempt_id"])["state"] == "acknowledged"
    reopened.close()


def test_cross_process_freeze_blocks_new_claims_but_not_results_or_reconciliation(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "claim-freeze.sqlite3"
    coordination = admission_coordination_path(state_path)
    app = RuntimeBackedHubV2App(state_path)
    app.admission_gate = AdmissionFreezeController(coordination)
    enrolled, capabilities, transport = _online_runtime_edge(app)
    identity = {
        "machine_id": "runtime-machine",
        "edge_generation": "runtime-generation",
        "contract_hash": capabilities["contract_hash"],
    }

    result_operation, result_dispatch, _ = _runtime_dispatch(
        app, transport, operation_key="result-during-freeze"
    )
    result_claim = transport.edge_claim(
        {**identity, "available_slots": 1, "max_attempts": 1, "lease_seconds": 30},
        token=enrolled["node_token"],
    )["attempt"]
    result_executing = transport.edge_lease(
        {
            **identity,
            "operation_id": result_claim["operation_id"],
            "attempt_id": result_claim["attempt_id"],
            "fencing_token": result_claim["fencing_token"],
            "expected_revision": result_claim["revision"],
            "lease_seconds": 30,
        },
        token=enrolled["node_token"],
    )["attempt"]

    reconcile_operation, _, reconcile_offer = _runtime_dispatch(
        app, transport, operation_key="reconcile-during-freeze"
    )
    reconcile_claim = app.broker.claim_attempt(
        reconcile_operation["operation_id"],
        reconcile_offer["attempt_id"],
        machine_id="runtime-machine",
        edge_generation=transport._generation_number("runtime-generation"),
        contract_hash=capabilities["contract_hash"],
        fencing_token=int(reconcile_offer["fencing_token"]),
        lease_seconds=30,
        principal_ref=app.store.principal_ref,
    )
    reconcile_executing = app.broker.mark_attempt_executing(
        reconcile_operation["operation_id"],
        reconcile_claim["attempt_id"],
        expected_revision=int(reconcile_claim["revision"]),
        machine_id="runtime-machine",
        edge_generation=transport._generation_number("runtime-generation"),
        contract_hash=capabilities["contract_hash"],
        fencing_token=int(reconcile_claim["fencing_token"]),
        principal_ref=app.store.principal_ref,
    )
    assert reconcile_executing is not None
    app.broker.expire_leases(
        now=time.time() + 3_600,
        operation_id=reconcile_operation["operation_id"],
    )

    pending_operation, _, pending_offer = _runtime_dispatch(
        app, transport, operation_key="must-not-claim-during-freeze"
    )
    backup_gate = AdmissionFreezeController(coordination)
    freeze = backup_gate.freeze_admissions(reason="backup:hub_v2")
    try:
        assert freeze.wait_for_drain(timeout_seconds=2) is True
        paused = transport.edge_claim(
            {**identity, "available_slots": 1, "max_attempts": 1},
            token=enrolled["node_token"],
        )
        assert paused["attempt"] is None
        assert paused["attempts"] == []
        assert paused["claim_paused"] is True
        assert app.store.get_attempt(pending_offer["attempt_id"])["state"] == "offered"

        completed = transport.edge_result(
            {
                **identity,
                "receipt": {
                    "receipt_id": "receipt-during-freeze",
                    "operation_id": result_operation["operation_id"],
                    "attempt_id": result_executing["attempt_id"],
                    "fencing_token": result_executing["fencing_token"],
                    "edge_generation": "runtime-generation",
                    "contract_hash": capabilities["contract_hash"],
                    "operation_payload_hash": result_dispatch["payload_hash"],
                    "outcome": "succeeded",
                    "result": {"accepted": True},
                },
            },
            token=enrolled["node_token"],
        )
        assert completed["operation"]["state"] == "succeeded"

        reconciled = transport.edge_reconcile(
            {
                **identity,
                "operation_id": reconcile_operation["operation_id"],
                "attempt_id": reconcile_executing["attempt_id"],
                "fencing_token": reconcile_executing["fencing_token"],
                "local_recovery": {
                    "found": False,
                    "recovery_action": "manual_recovery",
                },
            },
            token=enrolled["node_token"],
        )
        assert reconciled["disposition"] == "manual_recovery"
        assert reconciled["operation"]["state"] == "blocked"
    finally:
        freeze.release()

    claimed_after_release = transport.edge_claim(
        {**identity, "available_slots": 1, "max_attempts": 1},
        token=enrolled["node_token"],
    )["attempt"]
    assert claimed_after_release is not None
    assert claimed_after_release["operation_id"] == pending_operation["operation_id"]
    app.close()


def test_legacy_acknowledged_unknown_operation_is_recovered_as_terminal_blocker(
    tmp_path: Path,
) -> None:
    app = RuntimeBackedHubV2App(tmp_path / "legacy-uncertain-result.sqlite3")
    enrolled, capabilities, transport = _online_runtime_edge(app)
    operation, _, _ = _runtime_dispatch(
        app, transport, operation_key="legacy-uncertain-result"
    )
    identity = {
        "machine_id": "runtime-machine",
        "edge_generation": "runtime-generation",
        "contract_hash": capabilities["contract_hash"],
    }
    claimed = transport.edge_claim(
        {**identity, "available_slots": 1, "max_attempts": 1, "lease_seconds": 30},
        token=enrolled["node_token"],
    )["attempt"]
    executing = transport.edge_lease(
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
    pending = public_envelope(
        "pending", result={"reason": "outcome_unknown", "last_known_phase": "apply"}
    )
    internal_generation = app.store.get_attempt(executing["attempt_id"])[
        "edge_generation"
    ]
    result_ready = app.broker.transition_attempt(
        operation["operation_id"],
        executing["attempt_id"],
        expected_revision=executing["revision"],
        machine_id="runtime-machine",
        edge_generation=internal_generation,
        contract_hash=capabilities["contract_hash"],
        fencing_token=executing["fencing_token"],
        state="result_ready",
        result=pending,
    )
    assert result_ready is not None
    acknowledged = app.broker.acknowledge_result(
        operation["operation_id"],
        executing["attempt_id"],
        expected_revision=result_ready["revision"],
        machine_id="runtime-machine",
        edge_generation=internal_generation,
        contract_hash=capabilities["contract_hash"],
        fencing_token=executing["fencing_token"],
    )
    assert acknowledged is not None
    running = app.store.get_operation(operation["operation_id"])
    unknown = app.broker.transition_operation(
        operation["operation_id"],
        expected_revision=running["revision"],
        state="outcome_unknown",
        result=pending,
        error={"reason": "edge_outcome_unknown"},
    )
    assert unknown is not None
    requests = edge_reconciliation_requests(
        app.store, "runtime-machine", internal_generation
    )
    assert [request["attempt_id"] for request in requests] == [executing["attempt_id"]]

    recovered = transport.edge_reconcile(
        {
            **identity,
            "operation_id": operation["operation_id"],
            "attempt_id": executing["attempt_id"],
            "fencing_token": executing["fencing_token"],
            "local_recovery": {
                "found": False,
                "recovery_action": "manual_recovery",
            },
        },
        token=enrolled["node_token"],
    )

    saved_operation = app.store.get_operation(operation["operation_id"])
    assert recovered["disposition"] == "manual_recovery"
    assert recovered["operation"]["state"] == "blocked"
    assert saved_operation["state"] == "blocked"
    assert saved_operation["result"]["result"]["reason"] == (
        "edge_attempt_history_unavailable"
    )
    assert (
        edge_reconciliation_requests(app.store, "runtime-machine", internal_generation)
        == []
    )
    app.close()


def test_runtime_backed_server_retries_a_pre_effect_expired_lease(
    tmp_path: Path,
) -> None:
    app = RuntimeBackedHubV2App(tmp_path / "runtime-reconcile.sqlite3")
    old_contract = HUB_V2_CONTRACT_HASH
    current_contract = "runtime-current-contract"
    code = app.runtime.create_enrollment_code(name="runtime-edge", tags=["codex"])[
        "code"
    ]
    enrolled = app.runtime.enroll_machine(
        code=code,
        machine_id="runtime-machine",
        edge_generation="runtime-generation",
        display_name="Runtime Edge",
        tags=["codex"],
    )
    capabilities = {
        "contract_hash": old_contract,
        "action_capabilities": {"codex_worker_stop": "2"},
        "action_capability_versions": {"codex_worker_stop": "2"},
        "max_concurrent_jobs": 2,
        "queue_enabled": True,
    }
    app.runtime.heartbeat(
        machine_id="runtime-machine",
        token=enrolled["node_token"],
        edge_generation="runtime-generation",
        projection_revision=1,
        capabilities=capabilities,
        workspaces=[],
        resource_status={"active_workers": 0, "free_worker_slots": 2},
    )
    transport = HubPullTransportBridgeV2(app)
    payload = {
        "action": "codex_worker_stop",
        "arguments": {"worker": "Runtime Worker"},
        "machine_id": "runtime-machine",
        "edge_generation": "runtime-generation",
        "target": {
            "machine_id": "runtime-machine",
            "edge_generation": "runtime-generation",
        },
    }
    operation = app.broker.create_operation(
        tool="patchbay_worker_stop",
        logical_target="runtime-reconcile",
        idempotency_key="runtime-reconcile",
        payload=payload,
    )
    operation = app.broker.prepare_operation(
        operation["operation_id"], expected_revision=int(operation["revision"])
    )
    assert operation is not None
    operation = app.broker.make_dispatchable(
        operation["operation_id"], expected_revision=int(operation["revision"])
    )
    assert operation is not None
    dispatch = transport._persist_dispatch(operation, payload)
    transport._offer_dispatch(operation, dispatch)
    claimed = transport.edge_claim(
        {
            "machine_id": "runtime-machine",
            "edge_generation": "runtime-generation",
            "contract_hash": old_contract,
            "available_slots": 1,
            "max_attempts": 1,
            "lease_seconds": 1,
        },
        token=enrolled["node_token"],
    )["attempt"]
    assert claimed is not None
    app.broker.expire_leases(now=float(claimed["lease_expires_at"]) + 1)
    app.runtime.heartbeat(
        machine_id="runtime-machine",
        token=enrolled["node_token"],
        edge_generation="runtime-generation",
        projection_revision=2,
        capabilities={**capabilities, "contract_hash": current_contract},
        workspaces=[],
        resource_status={"active_workers": 0, "free_worker_slots": 2},
    )

    server = create_hub_v2_server(
        {"auth": {"enabled": False}},
        hub_app=app,
    )
    with TestClient(server) as client:
        response = client.post(
            "/edge/v2/reconcile",
            headers=_edge_headers(enrolled["node_token"]),
            json={
                "machine_id": "runtime-machine",
                "edge_generation": "runtime-generation",
                "session_contract_hash": current_contract,
                "contract": {
                    "contract_hash": current_contract,
                    "edge_generation": "runtime-generation",
                },
                "contract_hash": old_contract,
                "operation_id": claimed["operation_id"],
                "attempt_id": claimed["attempt_id"],
                "fencing_token": claimed["fencing_token"],
                "local_recovery": {
                    "recovery_action": "lease_reconciliation",
                    "found": False,
                    "effect_started": False,
                },
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["disposition"] == "retryable"
    assert body["retry_attempts"][0]["required_contract_hash"] == current_contract
    app.close()


def test_runtime_controller_hydrates_and_acknowledges_transient_payload(
    tmp_path: Path,
) -> None:
    app = RuntimeBackedHubV2App(tmp_path / "runtime-transient.sqlite3")
    enrolled, _, _ = _online_runtime_edge(app)
    server = create_hub_v2_server({"auth": {"enabled": False}}, hub_app=app)
    controller = server.state.hub_v2_edge_app
    operation = app.broker.create_operation(
        tool="patchbay_worker_inbox",
        logical_target="runtime-transient",
        idempotency_key="runtime-transient",
        payload={"intent": "import one transient artifact"},
    )
    download_url = "https://files.invalid/runtime-artifact?signature=ephemeral"
    transient = app.broker.register_payload(
        operation["operation_id"],
        payload_kind="artifact_download_url",
        checksum_sha256="a" * 64,
        size_bytes=123,
        storage_ref=download_url,
        expires_at=None,
        principal_ref=str(operation["principal_ref"]),
    )
    durable_payload = {
        "action": "codex_worker_inbox",
        "arguments": {
            "action": "import_file",
            "artifact_file": {
                "file_id": "runtime-file",
                "file_name": "runtime.txt",
                "mime_type": "text/plain",
            },
        },
        "transient_payload": {"payload_id": transient["payload_id"]},
        "machine_id": "runtime-machine",
        "edge_generation": "runtime-generation",
        "target": {
            "machine_id": "runtime-machine",
            "edge_generation": "runtime-generation",
        },
    }
    operation = app.store.get_operation(operation["operation_id"])
    assert operation is not None and operation["state"] == "payload_ready"
    operation = app.broker.make_dispatchable(
        operation["operation_id"], expected_revision=int(operation["revision"])
    )
    assert operation is not None
    dispatch = controller._persist_dispatch(operation, durable_payload)
    controller._offer_dispatch(operation, dispatch)

    identity = {
        "machine_id": "runtime-machine",
        "edge_generation": "runtime-generation",
        "contract_hash": HUB_V2_CONTRACT_HASH,
    }
    with TestClient(server) as client:
        claimed = client.post(
            "/edge/v2/claim",
            headers=_edge_headers(enrolled["node_token"]),
            json={**identity, "available_slots": 1, "max_attempts": 1},
        ).json()["attempt"]
        assert claimed["arguments"]["artifact_file"]["download_url"] == download_url
        assert "transient_payload" not in claimed["payload"]
        executing = client.post(
            "/edge/v2/lease",
            headers=_edge_headers(enrolled["node_token"]),
            json={
                **identity,
                "operation_id": claimed["operation_id"],
                "attempt_id": claimed["attempt_id"],
                "fencing_token": claimed["fencing_token"],
                "expected_revision": claimed["revision"],
            },
        ).json()["attempt"]
        receipt = {
            "receipt_id": "receipt-runtime-transient",
            "operation_id": claimed["operation_id"],
            "attempt_id": claimed["attempt_id"],
            "fencing_token": claimed["fencing_token"],
            "edge_generation": "runtime-generation",
            "contract_hash": HUB_V2_CONTRACT_HASH,
            "operation_payload_hash": claimed["operation_payload_hash"],
            "outcome": "succeeded",
            "result": {"accepted": True, "imported": True},
            "uncertain": False,
        }
        result = client.post(
            "/edge/v2/result",
            headers=_edge_headers(enrolled["node_token"]),
            json={**identity, "receipt": receipt},
        )

    assert result.status_code == 200
    assert executing["state"] == "executing"
    assert (
        app.store.get_payload_metadata(transient["payload_id"])["status"]
        == "acknowledged"
    )
    assert download_url not in str(
        app.store.get_entity("hub.edge_dispatch", operation["operation_id"])["record"]
    )
    saved_receipt = app.store.get_entity("hub.edge_receipt", receipt["receipt_id"])[
        "record"
    ]
    assert {
        key: saved_receipt[key]
        for key in (
            "receipt_id",
            "operation_id",
            "attempt_id",
            "fencing_token",
            "edge_generation",
            "machine_id",
            "contract_hash",
            "operation_payload_hash",
            "result_hash",
        )
    } == {
        "receipt_id": receipt["receipt_id"],
        "operation_id": claimed["operation_id"],
        "attempt_id": claimed["attempt_id"],
        "fencing_token": claimed["fencing_token"],
        "edge_generation": "runtime-generation",
        "machine_id": "runtime-machine",
        "contract_hash": HUB_V2_CONTRACT_HASH,
        "operation_payload_hash": claimed["operation_payload_hash"],
        "result_hash": saved_receipt["result_hash"],
    }
    app.close()


def test_runtime_controller_records_and_invalidates_group_preflight(
    tmp_path: Path,
) -> None:
    app = RuntimeBackedHubV2App(tmp_path / "runtime-preflight-state.sqlite3")
    enrolled, _, transport = _online_runtime_edge(app)
    group_id = "group_runtime_preflight"
    preflight_operation, _, _ = _runtime_dispatch(
        app,
        transport,
        operation_key=group_id,
        action="patchbay_edge_preflight",
        tool="patchbay_edge_preflight",
        arguments={"repo_path": "/repo", "include_tree": True},
        payload_fields={"work_group_id": group_id, "repo_path": "/repo"},
    )
    app.store.put_entity(
        "hub.work_group",
        group_id,
        {
            "work_group_id": group_id,
            "principal_ref": app.store.principal_ref,
            "title": "Runtime preflight parity",
            "goal": "Keep readiness aligned with base-checkout mutations.",
            "status": "open",
            "lifecycle": "open",
            "visibility": "private",
            "shared_write_policy": "serialized",
            "execution_mode": "end_to_end",
            "definition_of_done": "Preflight is recorded then invalidated.",
            "resolved_repo_path": "/repo",
            "pinned_machine_id": "runtime-machine",
            "pinned_edge_generation": "runtime-generation",
            "lanes": {},
            "participants": [],
            "readiness": {
                "status": "pending",
                "operation_id": preflight_operation["operation_id"],
            },
            "created_at": 1.0,
            "updated_at": 1.0,
        },
        expected_revision=0,
    )
    app.broker.associate_operation(
        preflight_operation["operation_id"], work_group_id=group_id, kind="preflight"
    )
    server = create_hub_v2_server({"auth": {"enabled": False}}, hub_app=app)
    headers = _edge_headers(enrolled["node_token"])
    identity = {
        "machine_id": "runtime-machine",
        "edge_generation": "runtime-generation",
        "contract_hash": HUB_V2_CONTRACT_HASH,
    }

    with TestClient(server) as client:
        claimed = client.post(
            "/edge/v2/claim",
            headers=headers,
            json={**identity, "available_slots": 1, "max_attempts": 1},
        ).json()["attempt"]
        assert claimed["action"] == "codex_open_workspace"
        assert claimed["arguments"] == {
            "repo": "/repo",
            "include_tree": False,
            "include_skills": False,
        }
        executing = client.post(
            "/edge/v2/lease",
            headers=headers,
            json={
                **identity,
                "operation_id": claimed["operation_id"],
                "attempt_id": claimed["attempt_id"],
                "fencing_token": claimed["fencing_token"],
                "expected_revision": claimed["revision"],
            },
        ).json()["attempt"]
        preflight_receipt = {
            "receipt_id": "receipt-runtime-preflight",
            "operation_id": executing["operation_id"],
            "attempt_id": executing["attempt_id"],
            "fencing_token": executing["fencing_token"],
            "edge_generation": "runtime-generation",
            "contract_hash": HUB_V2_CONTRACT_HASH,
            "operation_payload_hash": claimed["operation_payload_hash"],
            "outcome": "succeeded",
            "result": {
                "ok": True,
                "repo_exists": True,
                "repo_resolved": "/repo",
                "head": "abc123",
                "disk_free_bytes": 10_000_000_000,
                "free_worker_slots": 2,
                "queue_enabled": True,
            },
            "uncertain": False,
        }
        completed = client.post(
            "/edge/v2/result",
            headers=headers,
            json={**identity, "receipt": preflight_receipt},
        )
        assert completed.status_code == 200

        readiness = app.store.get_entity("hub.work_group", group_id)["record"][
            "readiness"
        ]
        assert readiness["status"] == "ready"
        assert readiness["currentness"] == "current"
        assert readiness["facts_revision"] == "abc123"

        shared_operation, _, _ = _runtime_dispatch(
            app,
            transport,
            operation_key="runtime-shared-writer",
            action="codex_worker_start",
            tool="patchbay_worker_start",
            arguments={
                "name": "Runtime Shared Writer",
                "brief": "Exercise base-checkout readiness invalidation.",
                "repo": "/repo",
                "workspace_mode": "shared_write",
            },
            payload_fields={"work_group_id": group_id},
        )
        app.broker.associate_operation(
            shared_operation["operation_id"], work_group_id=group_id, kind="worker"
        )
        writer = client.post(
            "/edge/v2/claim",
            headers=headers,
            json={**identity, "available_slots": 1, "max_attempts": 1},
        ).json()["attempt"]
        writer_executing = client.post(
            "/edge/v2/lease",
            headers=headers,
            json={
                **identity,
                "operation_id": writer["operation_id"],
                "attempt_id": writer["attempt_id"],
                "fencing_token": writer["fencing_token"],
                "expected_revision": writer["revision"],
            },
        ).json()["attempt"]
        writer_receipt = {
            "receipt_id": "receipt-runtime-shared-writer",
            "operation_id": writer_executing["operation_id"],
            "attempt_id": writer_executing["attempt_id"],
            "fencing_token": writer_executing["fencing_token"],
            "edge_generation": "runtime-generation",
            "contract_hash": HUB_V2_CONTRACT_HASH,
            "operation_payload_hash": writer["operation_payload_hash"],
            "outcome": "succeeded",
            "result": {"accepted": True, "worker_id": "worker-runtime"},
            "uncertain": False,
        }
        writer_result = client.post(
            "/edge/v2/result",
            headers=headers,
            json={**identity, "receipt": writer_receipt},
        )
        assert writer_result.status_code == 200

    readiness = app.store.get_entity("hub.work_group", group_id)["record"]["readiness"]
    assert readiness["status"] == "ready"
    assert readiness["currentness"] == "refresh_required"
    assert readiness["stale_reason"] == "shared_write_worker_can_change_base_checkout"
    assert readiness["stale_source_operation_id"] == shared_operation["operation_id"]
    app.close()


def test_runtime_controller_returns_resumable_attempt_after_edge_restart(
    tmp_path: Path,
) -> None:
    app = RuntimeBackedHubV2App(tmp_path / "runtime-resume.sqlite3")
    enrolled, _, transport = _online_runtime_edge(app)
    _, _, _ = _runtime_dispatch(app, transport, operation_key="runtime-resume")
    server = create_hub_v2_server({"auth": {"enabled": False}}, hub_app=app)
    identity = {
        "machine_id": "runtime-machine",
        "edge_generation": "runtime-generation",
        "contract_hash": HUB_V2_CONTRACT_HASH,
    }
    with TestClient(server) as client:
        claimed = client.post(
            "/edge/v2/claim",
            headers=_edge_headers(enrolled["node_token"]),
            json={**identity, "available_slots": 1, "max_attempts": 1},
        ).json()["attempt"]
        executing = client.post(
            "/edge/v2/lease",
            headers=_edge_headers(enrolled["node_token"]),
            json={
                **identity,
                "operation_id": claimed["operation_id"],
                "attempt_id": claimed["attempt_id"],
                "fencing_token": claimed["fencing_token"],
                "expected_revision": claimed["revision"],
            },
        ).json()["attempt"]
        recovered = client.post(
            "/edge/v2/reconcile",
            headers=_edge_headers(enrolled["node_token"]),
            json={
                **identity,
                "operation_id": executing["operation_id"],
                "attempt_id": executing["attempt_id"],
                "fencing_token": executing["fencing_token"],
                "local_recovery": {
                    "found": True,
                    "recovery_action": "execute_intent",
                    "effect_started": False,
                },
            },
        )

    assert recovered.status_code == 200
    resume = recovered.json()["resume_attempts"]
    assert [item["attempt_id"] for item in resume] == [executing["attempt_id"]]
    assert resume[0]["state"] == "executing"
    assert resume[0]["action"] == "codex_worker_stop"
    app.close()


def test_transport_rotates_successor_contract_and_action_version_before_execution(
    tmp_path: Path,
) -> None:
    app = RuntimeBackedHubV2App(tmp_path / "action-roll.sqlite3")
    enrolled, capabilities, transport = _online_runtime_edge(app)
    operation, dispatch, old_attempt = _runtime_dispatch(
        app, transport, operation_key="action-roll-v2-v3"
    )
    current_contract = "edge-contract-v3"
    app.runtime.heartbeat(
        machine_id="runtime-machine",
        token=enrolled["node_token"],
        edge_generation="runtime-generation",
        projection_revision=2,
        capabilities={
            **capabilities,
            "contract_hash": current_contract,
            "action_capabilities": {"codex_worker_stop": "3"},
            "action_capability_versions": {"codex_worker_stop": "3"},
        },
        workspaces=[],
        resource_status={"active_workers": 0, "free_worker_slots": 2},
    )
    identity = {
        "machine_id": "runtime-machine",
        "edge_generation": "runtime-generation",
        "contract_hash": current_contract,
    }
    claimed = transport.edge_claim(
        {**identity, "available_slots": 1, "max_attempts": 1, "lease_seconds": 30},
        token=enrolled["node_token"],
    )["attempt"]
    assert claimed is not None
    assert claimed["attempt_id"] != old_attempt["attempt_id"]
    assert claimed["required_contract_hash"] == current_contract
    assert claimed["required_action_capability_version"] == "3"
    executing = transport.edge_lease(
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
    receipt = {
        "receipt_id": "receipt-action-v3",
        "operation_id": claimed["operation_id"],
        "attempt_id": claimed["attempt_id"],
        "fencing_token": claimed["fencing_token"],
        "edge_generation": "runtime-generation",
        "contract_hash": current_contract,
        "operation_payload_hash": dispatch["payload_hash"],
        "outcome": "succeeded",
        "result": {"accepted": True, "stopped": True},
        "error": "",
        "uncertain": False,
    }
    transport.edge_result(
        {**identity, "receipt": receipt}, token=enrolled["node_token"]
    )

    attempts = app.store.connection.execute(
        "SELECT state FROM attempts WHERE operation_id = ? ORDER BY fencing_token",
        (operation["operation_id"],),
    ).fetchall()
    saved_dispatch = app.store.get_entity(
        "hub.edge_dispatch", operation["operation_id"]
    )
    assert [str(row["state"]) for row in attempts] == ["retryable", "acknowledged"]
    assert app.store.get_operation(operation["operation_id"])["state"] == "succeeded"
    assert saved_dispatch["record"]["required_action_capability_version"] == "3"
    assert executing["state"] == "executing"
    app.close()


def test_contract_rotation_without_action_support_becomes_terminal_blocker(
    tmp_path: Path,
) -> None:
    app = RuntimeBackedHubV2App(tmp_path / "incompatible-action.sqlite3")
    enrolled, capabilities, transport = _online_runtime_edge(app)
    operation, _, _ = _runtime_dispatch(
        app, transport, operation_key="incompatible-action"
    )
    current_contract = "edge-contract-without-stop"
    app.runtime.heartbeat(
        machine_id="runtime-machine",
        token=enrolled["node_token"],
        edge_generation="runtime-generation",
        projection_revision=2,
        capabilities={
            **capabilities,
            "contract_hash": current_contract,
            "action_capabilities": {},
            "action_capability_versions": {},
        },
        workspaces=[],
        resource_status={"active_workers": 0, "free_worker_slots": 2},
    )
    claim = transport.edge_claim(
        {
            "machine_id": "runtime-machine",
            "edge_generation": "runtime-generation",
            "contract_hash": current_contract,
            "available_slots": 1,
            "max_attempts": 1,
        },
        token=enrolled["node_token"],
    )

    saved = app.store.get_operation(operation["operation_id"])
    assert claim["attempt"] is None
    assert saved["state"] == "blocked"
    assert saved["result"]["result"]["reason"] == "edge_action_capability_mismatch"
    assert (
        "start a new manager operation" in saved["result"]["result"]["manager_guidance"]
    )
    assert (
        app.store.get_entity("hub.edge_dispatch", operation["operation_id"])["record"][
            "status"
        ]
        == "blocked"
    )
    app.close()


def test_missing_edge_journal_history_terminally_blocks_reconciliation(
    tmp_path: Path,
) -> None:
    app = RuntimeBackedHubV2App(tmp_path / "missing-history.sqlite3")
    enrolled, _, transport = _online_runtime_edge(app)
    operation, _, _ = _runtime_dispatch(app, transport, operation_key="missing-history")
    identity = {
        "machine_id": "runtime-machine",
        "edge_generation": "runtime-generation",
        "contract_hash": HUB_V2_CONTRACT_HASH,
    }
    claimed = transport.edge_claim(
        {**identity, "available_slots": 1, "max_attempts": 1, "lease_seconds": 1},
        token=enrolled["node_token"],
    )["attempt"]
    assert claimed is not None
    app.broker.expire_leases(now=float(claimed["lease_expires_at"]) + 1)

    reconciled = transport.edge_reconcile(
        {
            **identity,
            "operation_id": claimed["operation_id"],
            "attempt_id": claimed["attempt_id"],
            "fencing_token": claimed["fencing_token"],
            "local_recovery": {"found": False},
        },
        token=enrolled["node_token"],
    )

    saved = app.store.get_operation(operation["operation_id"])
    assert reconciled["accepted"] is True
    assert reconciled["found"] is False
    assert saved["state"] == "blocked"
    assert saved["result"]["result"]["reason"] == "edge_attempt_history_unavailable"
    assert (
        "start a new manager operation" in saved["result"]["result"]["manager_guidance"]
    )
    assert (
        transport._reconciliation_requests("runtime-machine", "runtime-generation")
        == []
    )
    app.close()


def test_runtime_controller_heartbeat_drives_receipt_and_reconciliation_flow(
    tmp_path: Path,
) -> None:
    app = RuntimeBackedHubV2App(tmp_path / "runtime-heartbeat-control.sqlite3")
    enrolled, capabilities, transport = _online_runtime_edge(app)
    first_operation, first_dispatch, _ = _runtime_dispatch(
        app, transport, operation_key="heartbeat-receipt"
    )
    server = create_hub_v2_server({"auth": {"enabled": False}}, hub_app=app)
    headers = _edge_headers(enrolled["node_token"])
    identity = {
        "machine_id": "runtime-machine",
        "edge_generation": "runtime-generation",
        "contract_hash": HUB_V2_CONTRACT_HASH,
    }
    with TestClient(server) as client:
        first_claim = client.post(
            "/edge/v2/claim",
            headers=headers,
            json={**identity, "available_slots": 1, "max_attempts": 1},
        ).json()["attempt"]
        first_execution = client.post(
            "/edge/v2/lease",
            headers=headers,
            json={
                **identity,
                "operation_id": first_claim["operation_id"],
                "attempt_id": first_claim["attempt_id"],
                "fencing_token": first_claim["fencing_token"],
                "expected_revision": first_claim["revision"],
            },
        ).json()["attempt"]
        receipt = {
            "receipt_id": "receipt-heartbeat-control",
            "operation_id": first_claim["operation_id"],
            "attempt_id": first_claim["attempt_id"],
            "fencing_token": first_claim["fencing_token"],
            "edge_generation": "runtime-generation",
            "contract_hash": HUB_V2_CONTRACT_HASH,
            "operation_payload_hash": first_dispatch["payload_hash"],
            "result": {"accepted": True, "stopped": True},
        }
        finished = client.post(
            "/edge/v2/result",
            headers=headers,
            json={**identity, "receipt": receipt},
        )
        assert finished.status_code == 200
        assert first_execution["state"] == "executing"
        assert (
            app.store.get_operation(first_operation["operation_id"])["state"]
            == "succeeded"
        )

        second_operation, _, _ = _runtime_dispatch(
            app,
            transport,
            operation_key="heartbeat-reconciliation",
            action="patchbay_edge_preflight",
            tool="patchbay_edge_preflight",
            arguments={"repo_path": "/repo", "include_tree": True},
        )
        second_claim = client.post(
            "/edge/v2/claim",
            headers=headers,
            json={**identity, "available_slots": 1, "max_attempts": 1},
        ).json()["attempt"]
        assert second_claim["action"] == "codex_open_workspace"
        assert second_claim["arguments"] == {
            "repo": "/repo",
            "include_tree": False,
            "include_skills": False,
        }
        app.broker.expire_leases(now=float(second_claim["lease_expires_at"]) + 1)
        heartbeat = client.post(
            "/edge/v2/heartbeat",
            headers=headers,
            json={
                **identity,
                "projection_revision": 2,
                "capabilities": capabilities,
                "workspaces": [],
                "resource_status": {"active_workers": 0, "free_worker_slots": 2},
            },
        ).json()
        assert (
            heartbeat["receipt_acknowledgements"][0]["receipt_id"]
            == receipt["receipt_id"]
        )
        assert (
            heartbeat["reconciliation_requests"][0]["attempt_id"]
            == second_claim["attempt_id"]
        )

        acknowledged = client.post(
            "/edge/v2/outbox/ack",
            headers=headers,
            json={**identity, "receipt_ids": [receipt["receipt_id"]]},
        )
        assert acknowledged.status_code == 200
        reconciled = client.post(
            "/edge/v2/reconcile",
            headers=headers,
            json={
                **identity,
                "operation_id": second_claim["operation_id"],
                "attempt_id": second_claim["attempt_id"],
                "fencing_token": second_claim["fencing_token"],
                "local_recovery": {"found": False},
            },
        )
        assert reconciled.status_code == 200
        assert (
            app.store.get_operation(second_operation["operation_id"])["state"]
            == "blocked"
        )

        settled = client.post(
            "/edge/v2/heartbeat",
            headers=headers,
            json={
                **identity,
                "projection_revision": 3,
                "capabilities": capabilities,
                "workspaces": [],
                "resource_status": {"active_workers": 0, "free_worker_slots": 2},
            },
        ).json()
        assert "receipt_acknowledgements" not in settled
        assert "reconciliation_requests" not in settled
    app.close()


def test_default_production_factory_exposes_complete_v2_surface(tmp_path: Path) -> None:
    config = {
        "auth": {"enabled": False},
        "hub": {
            "control_plane": "v2",
            "state_db": str(tmp_path / "hub-v2.sqlite3"),
        },
        "pro_requests": {"root": str(tmp_path / "pro-requests")},
        "repositories": {"allowed": [str(tmp_path)]},
    }
    server = create_hub_v2_server(config)
    with TestClient(server) as client:
        initialized = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "factory-test", "version": "1"},
                },
            },
        )
        assert initialized.status_code == 200
        session_id = initialized.headers["Mcp-Session-Id"]
        listed = client.post(
            "/mcp",
            headers={"Mcp-Session-Id": session_id},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert listed.status_code == 200
        names = tuple(tool["name"] for tool in listed.json()["result"]["tools"])
        assert names == HUB_V2_TOOL_NAMES
        assert len(names) == HUB_V2_EXPECTED_TOOL_COUNT == 31
        pro_requests = client.post(
            "/mcp",
            headers={"Mcp-Session-Id": session_id},
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "patchbay_pro_request_list",
                    "arguments": {"limit": 10},
                },
            },
        )
        assert pro_requests.status_code == 200
        structured = pro_requests.json()["result"]["structuredContent"]
        assert structured["status"] == "ok"
        assert structured["result"]["requests"] == []


def test_server_lifespan_owns_explicit_pending_dispatch_recovery() -> None:
    class RecoveryDispatchApp(StatefulHubV2App):
        def __init__(self) -> None:
            super().__init__()
            self.recovery_called = threading.Event()
            self.recovery_calls = 0

        async def dispatch_pending_operations(
            self,
            *,
            context: RequestContext | None = None,
            max_operations: int = 100,
        ) -> list[str]:
            del context
            assert max_operations == 7
            self.recovery_calls += 1
            self.recovery_called.set()
            return []

    app = RecoveryDispatchApp()
    server = create_hub_v2_server(
        {
            "auth": {"enabled": False},
            "hub": {
                "recovery_dispatch_interval_seconds": 0.1,
                "recovery_dispatch_batch_size": 7,
            },
        },
        hub_app=app,
    )

    with TestClient(server):
        assert app.recovery_called.wait(timeout=2)
        assert app.recovery_calls >= 1

    stopped_at = app.recovery_calls
    time.sleep(0.2)
    assert app.recovery_calls == stopped_at
