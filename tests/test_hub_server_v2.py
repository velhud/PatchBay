from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from fastapi.testclient import TestClient

from patchbay.hub.broker import OperationBroker
from patchbay.hub.edge import build_capabilities
from patchbay.hub.edge_client_v2 import edge_contract_metadata
from patchbay.hub.edge_journal import EdgeJournal
from patchbay.hub.edge_v2 import EdgeExecutionService
from patchbay.hub.operations import public_envelope
from patchbay.hub.runtime_v2 import HubRuntimeV2
from patchbay.hub.server_v2 import create_hub_v2_server
from patchbay.hub.store_v2 import HubStoreV2, HubStoreV2Conflict, semantic_payload_hash
from patchbay.hub.tool_surface import HUB_V2_EXPECTED_TOOL_COUNT, HUB_V2_TOOL_NAMES
from patchbay.protocol.context import RequestContext


OPERATOR_TOKEN = "operator-http-token"
EDGE_CONTRACT = "contract-v2-test"


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


def _server(
    app: StatefulHubV2App,
    *,
    max_request_bytes: int = 4096,
    inject_factory: bool = False,
):
    dependency = {"hub_app_factory": lambda: app} if inject_factory else {"hub_app": app}
    return create_hub_v2_server(
        {
            "server": {"host": "0.0.0.0", "max_request_bytes": max_request_bytes},
            "auth": {"enabled": True},
        },
        environ={"PATCHBAY_HTTP_TOKEN": OPERATOR_TOKEN},
        **dependency,
    )


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
    assert client.get("/status", headers=_operator_headers()).json()["principal_ref"] == app.principal_ref
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401

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
    assert client.post(
        "/edge/v2/heartbeat", headers=_edge_headers(first_token), json=heartbeat
    ).status_code == 200

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

    stale_receipt = {**receipt, "attempt_id": "attempt_stale", "result": {"status": "failed"}}
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
            "projection": {"snapshot_kind": "delta", "workers": [{"worker_id": "wrong"}]},
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


def test_runtime_backed_edge_claim_result_and_stale_attempt_are_fenced(tmp_path: Path) -> None:
    state_path = tmp_path / "hub-v2.sqlite3"
    app = RuntimeBackedHubV2App(state_path)
    principal_ref = app.store.principal_ref
    enrollment = app.runtime.create_enrollment_code(name="Runtime Edge")
    client = TestClient(_server(app))
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
    app.store.put_entity(
        "hub.edge_dispatch",
        operation["operation_id"],
        {
            "operation_id": operation["operation_id"],
            "payload": dispatch_payload,
            "payload_hash": semantic_payload_hash(dispatch_payload),
            "status": "pending",
            "created_at": operation["created_at"],
        },
        expected_revision=0,
    )

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
    assert finished.json()["receipt_acknowledgements"][0]["receipt_id"] == "receipt_runtime_1"

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
