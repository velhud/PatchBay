from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

import pytest

from patchbay.hub.app_v2 import (
    EDGE_DISPATCH_ENTITY,
    EdgeDeliveryBridgeV2,
    HubBrokerEdgeDispatchPortV2,
)
from patchbay.hub.broker import OperationBroker
from patchbay.hub.operations import public_envelope
from patchbay.hub.runtime_v2 import MACHINE_ENTITY, HubRuntimeV2
from patchbay.hub.store_v2 import HubStoreV2, semantic_payload_hash
from patchbay.hub.tool_surface import HUB_V2_CONTRACT_HASH
from patchbay.hub.transport_v2 import (
    EDGE_RECEIPT_ENTITY,
    HubPullTransportBridgeV2,
)


class RecordingEdge:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(deepcopy(kwargs))
        return {"accepted": True}


class BoundServices:
    def __init__(
        self, store: HubStoreV2, broker: OperationBroker, runtime: HubRuntimeV2
    ) -> None:
        self.store = store
        self.broker = broker
        self.runtime = runtime


def _operation(
    broker: OperationBroker,
    *,
    operation_id: str,
    payload: Mapping[str, Any],
    terminal: bool = False,
) -> dict[str, Any]:
    operation = broker.create_operation(
        tool="patchbay_worker_stop",
        logical_target=operation_id,
        idempotency_key=f"key-{operation_id}",
        payload=payload,
        operation_id=operation_id,
    )
    operation = broker.prepare_operation(
        operation_id, expected_revision=int(operation["revision"])
    )
    assert operation is not None
    operation = broker.make_dispatchable(
        operation_id, expected_revision=int(operation["revision"])
    )
    assert operation is not None
    if terminal:
        operation = broker.transition_operation(
            operation_id,
            expected_revision=int(operation["revision"]),
            state="running",
        )
        assert operation is not None
        operation = broker.transition_operation(
            operation_id,
            expected_revision=int(operation["revision"]),
            state="succeeded",
            result=public_envelope("ok"),
        )
        assert operation is not None
    return operation


def _dispatch_record(
    store: HubStoreV2,
    operation: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> None:
    store.put_entity(
        EDGE_DISPATCH_ENTITY,
        str(operation["operation_id"]),
        {
            "operation_id": str(operation["operation_id"]),
            "action": str(payload["action"]),
            "payload": deepcopy(dict(payload)),
            "payload_hash": semantic_payload_hash(payload),
            "status": "pending",
            "created_at": float(operation["created_at"]),
        },
        expected_revision=0,
    )


@pytest.mark.asyncio
async def test_terminal_dispatch_history_cannot_starve_new_work(tmp_path) -> None:
    store = HubStoreV2(tmp_path / "dispatch-history.sqlite3")
    broker = OperationBroker(store)
    runtime = HubRuntimeV2(store, broker=broker)
    edge = RecordingEdge()
    port = HubBrokerEdgeDispatchPortV2(
        broker, runtime, EdgeDeliveryBridgeV2(edge)
    )
    payload = {
        "action": "codex_worker_stop",
        "arguments": {"worker": "worker-old"},
        "target": {"machine_id": "machine-1", "edge_generation": "gen-1"},
    }

    for index in range(101):
        operation = _operation(
            broker,
            operation_id=f"op-terminal-{index:03d}",
            payload=payload,
            terminal=True,
        )
        _dispatch_record(store, operation, payload)

    pending_payload = {
        **payload,
        "arguments": {"worker": "worker-new"},
    }
    pending = _operation(
        broker,
        operation_id="op-new-work",
        payload=pending_payload,
    )
    _dispatch_record(store, pending, pending_payload)

    delivered = await port.dispatch_pending(max_operations=1)

    assert delivered == ["op-new-work"]
    assert [call["arguments"]["worker"] for call in edge.calls] == ["worker-new"]
    assert store.get_operation("op-new-work")["state"] == "succeeded"
    store.close()


def test_receipt_acknowledgements_page_and_retire_beyond_100(tmp_path) -> None:
    store = HubStoreV2(tmp_path / "receipt-history.sqlite3")
    broker = OperationBroker(store)
    runtime = HubRuntimeV2(store, broker=broker)
    transport = HubPullTransportBridgeV2(BoundServices(store, broker, runtime))
    transport._authenticate = lambda payload, token, require_contract: {}  # type: ignore[method-assign]

    for index in range(101):
        transport._record_receipt(
            {
                "receipt_id": f"receipt-{index:03d}",
                "operation_id": f"operation-{index:03d}",
                "attempt_id": f"attempt-{index:03d}",
                "fencing_token": index + 1,
                "edge_generation": "generation-1",
            },
            machine_id="machine-1",
            contract_hash="contract-1",
            operation_payload_hash=f"payload-{index:03d}",
            result_hash=f"result-{index:03d}",
        )

    identity = {"machine_id": "machine-1", "edge_generation": "generation-1"}
    first_page = transport._control_response({}, identity)["receipt_acknowledgements"]
    assert len(first_page) == 100
    assert first_page[0]["receipt_id"] == "receipt-000"
    assert first_page[-1]["receipt_id"] == "receipt-099"

    retired = transport.edge_outbox_ack(
        {**identity, "receipt_ids": [item["receipt_id"] for item in first_page]},
        token="ignored",
    )
    assert retired["accepted"] is True
    assert len(retired["acknowledged_receipts"]) == 100
    assert store.get_entity(EDGE_RECEIPT_ENTITY, "receipt-000")["record"]["status"] == "retired"

    second_page = transport._control_response({}, identity)["receipt_acknowledgements"]
    assert [item["receipt_id"] for item in second_page] == ["receipt-100"]
    assert transport.edge_outbox_ack(
        {**identity, "receipt_ids": ["receipt-100"]}, token="ignored"
    )["accepted"] is True
    assert "receipt_acknowledgements" not in transport._control_response({}, identity)

    replay = transport.edge_outbox_ack(
        {**identity, "receipt_ids": ["receipt-100"]}, token="ignored"
    )
    assert replay["accepted"] is True
    assert replay["acknowledged_receipts"][0]["receipt_id"] == "receipt-100"
    store.close()


@pytest.mark.asyncio
async def test_artifact_download_url_uses_transient_payload_lifecycle(tmp_path) -> None:
    store = HubStoreV2(tmp_path / "transient-artifact.sqlite3")
    broker = OperationBroker(store)
    runtime = HubRuntimeV2(store, broker=broker)
    edge = RecordingEdge()
    port = HubBrokerEdgeDispatchPortV2(
        broker, runtime, EdgeDeliveryBridgeV2(edge)
    )
    download_url = "https://files.invalid/temporary-artifact?signature=short-lived"
    payload = {
        "action": "codex_worker_inbox",
        "arguments": {
            "action": "import_file",
            "artifact_file": {
                "download_url": download_url,
                "file_id": "file-1",
                "file_name": "input.txt",
                "mime_type": "text/plain",
            },
        },
        "target": {"machine_id": "machine-1", "edge_generation": "gen-1"},
    }
    operation = port.create_operation(
        tool="patchbay_worker_inbox",
        logical_target="artifact-inbox",
        idempotency_key="artifact-inbox-1",
        payload=payload,
    )
    dispatch = store.get_entity(EDGE_DISPATCH_ENTITY, operation["operation_id"])
    assert dispatch is not None
    durable_payload = dispatch["record"]["payload"]
    assert download_url not in str(durable_payload)
    payload_id = durable_payload["transient_payload"]["payload_id"]
    metadata = store.get_payload_metadata(payload_id)
    assert metadata is not None
    assert metadata["storage_ref"] == download_url
    assert metadata["status"] == "ready"

    replay = port.create_operation(
        tool="patchbay_worker_inbox",
        logical_target="artifact-inbox",
        idempotency_key="artifact-inbox-1",
        payload=payload,
    )
    assert replay["operation_id"] == operation["operation_id"]

    store.put_entity(
        MACHINE_ENTITY,
        "machine-1",
        {
            "machine_id": "machine-1",
            "edge_generation": "gen-1",
            "capabilities": {
                "contract_hash": HUB_V2_CONTRACT_HASH,
                "action_capabilities": {"codex_worker_inbox": "v1"},
            },
        },
        expected_revision=0,
    )
    pull_transport = HubPullTransportBridgeV2(BoundServices(store, broker, runtime))
    persisted = pull_transport._persist_dispatch(operation, durable_payload)
    assert persisted["payload"] == durable_payload
    hydrated = pull_transport._hydrate_transient_payload(durable_payload)
    assert hydrated["arguments"]["artifact_file"]["download_url"] == download_url
    assert "transient_payload" not in hydrated

    operation = broker.make_dispatchable(
        operation["operation_id"], expected_revision=int(operation["revision"])
    )
    assert operation is not None
    assert await port.dispatch_pending(max_operations=1) == [operation["operation_id"]]
    assert edge.calls[0]["arguments"]["artifact_file"]["download_url"] == download_url
    assert store.get_payload_metadata(payload_id)["status"] == "acknowledged"
    assert download_url not in str(
        store.get_entity(EDGE_DISPATCH_ENTITY, operation["operation_id"])["record"]
    )
    store.close()
