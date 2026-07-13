from __future__ import annotations

import json
import time
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
from patchbay.hub.runtime_v2 import (
    MACHINE_ENTITY,
    OPERATION_GROUP_ENTITY,
    WORK_GROUP_ENTITY,
    HubRuntimeV2,
)
import patchbay.hub.store_v2 as store_v2_module
from patchbay.hub.store_v2 import HubStoreV2, semantic_payload_hash
from patchbay.hub.tool_surface import HUB_V2_CONTRACT_HASH
from patchbay.hub.transport_v2 import (
    EDGE_RECEIPT_ENTITY,
    HubPullTransportBridgeV2,
    edge_receipt_acknowledgements,
    edge_reconciliation_requests,
)
from patchbay.protocol.context import RequestContext


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


def test_twenty_thousand_retired_receipts_and_dispatches_are_index_bounded(
    tmp_path,
) -> None:
    store = HubStoreV2(tmp_path / "control-history.sqlite3")
    broker = OperationBroker(store)
    runtime = HubRuntimeV2(store, broker=broker)
    transport = HubPullTransportBridgeV2(BoundServices(store, broker, runtime))
    history_size = 20_000
    machine_id = "machine-history"
    edge_generation = "generation-history"
    generation_number = transport._generation_number(edge_generation)
    principal_ref = store.principal_ref

    with store.immediate_transaction() as connection:
        receipt_entities = []
        receipt_index = []
        dispatch_operations = []
        dispatch_entities = []
        dispatch_index = []
        for index in range(history_size):
            created_at = float(index)
            receipt_id = f"receipt-retired-{index:05d}"
            receipt_record = {
                "receipt_id": receipt_id,
                "operation_id": f"receipt-operation-{index:05d}",
                "attempt_id": f"receipt-attempt-{index:05d}",
                "fencing_token": 1,
                "machine_id": machine_id,
                "edge_generation": edge_generation,
                "status": "retired",
                "created_at": created_at,
            }
            receipt_entities.append(
                (
                    EDGE_RECEIPT_ENTITY,
                    receipt_id,
                    json.dumps(receipt_record, separators=(",", ":")),
                    created_at,
                    created_at,
                )
            )
            receipt_index.append(
                (
                    EDGE_RECEIPT_ENTITY,
                    receipt_id,
                    machine_id,
                    edge_generation,
                    "retired",
                    created_at,
                )
            )

            operation_id = f"dispatch-retired-{index:05d}"
            dispatch_record = {
                "operation_id": operation_id,
                "machine_id": machine_id,
                "edge_generation": edge_generation,
                "status": "complete",
                "created_at": created_at,
            }
            dispatch_operations.append(
                (
                    operation_id,
                    principal_ref,
                    "patchbay_worker_stop",
                    operation_id,
                    f"history-{index:05d}",
                    "0" * 64,
                    "succeeded",
                    created_at,
                    created_at,
                )
            )
            dispatch_entities.append(
                (
                    EDGE_DISPATCH_ENTITY,
                    operation_id,
                    json.dumps(dispatch_record, separators=(",", ":")),
                    created_at,
                    created_at,
                )
            )
            dispatch_index.append(
                (
                    EDGE_DISPATCH_ENTITY,
                    operation_id,
                    machine_id,
                    edge_generation,
                    "complete",
                    created_at,
                )
            )
        connection.executemany(
            """
            INSERT INTO operations
                (operation_id, principal_ref, tool, logical_target, idempotency_key,
                 semantic_payload_hash, state, revision, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            dispatch_operations,
        )
        connection.executemany(
            """
            INSERT INTO entity_records
                (entity_type, entity_id, revision, record_json,
                 legacy_classification, source_import_id, created_at, updated_at)
            VALUES (?, ?, 1, ?, '', NULL, ?, ?)
            """,
            receipt_entities + dispatch_entities,
        )
        connection.executemany(
            """
            INSERT INTO entity_control_index
                (entity_type, entity_id, machine_id, edge_generation, status,
                 sort_created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            receipt_index + dispatch_index,
        )

        pending_receipt = {
            "receipt_id": "receipt-pending",
            "operation_id": "receipt-operation-pending",
            "attempt_id": "receipt-attempt-pending",
            "fencing_token": 1,
            "machine_id": machine_id,
            "edge_generation": edge_generation,
            "status": "pending",
            "created_at": history_size + 1.0,
        }
        connection.execute(
            """
            INSERT INTO entity_records
                (entity_type, entity_id, revision, record_json,
                 legacy_classification, source_import_id, created_at, updated_at)
            VALUES (?, ?, 1, ?, '', NULL, ?, ?)
            """,
            (
                EDGE_RECEIPT_ENTITY,
                "receipt-pending",
                json.dumps(pending_receipt, separators=(",", ":")),
                history_size + 1.0,
                history_size + 1.0,
            ),
        )
        connection.execute(
            """
            INSERT INTO entity_control_index
                (entity_type, entity_id, machine_id, edge_generation, status,
                 sort_created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (
                EDGE_RECEIPT_ENTITY,
                "receipt-pending",
                machine_id,
                edge_generation,
                history_size + 1.0,
            ),
        )

        active_operation = "dispatch-active"
        connection.execute(
            """
            INSERT INTO operations
                (operation_id, principal_ref, tool, logical_target, idempotency_key,
                 semantic_payload_hash, state, revision, created_at, updated_at)
            VALUES (?, ?, 'patchbay_worker_stop', ?, 'active-key', ?, 'running', 1, ?, ?)
            """,
            (
                active_operation,
                principal_ref,
                active_operation,
                "1" * 64,
                history_size + 2.0,
                history_size + 2.0,
            ),
        )
        connection.execute(
            """
            INSERT INTO attempts
                (attempt_id, operation_id, machine_id, edge_generation,
                 fencing_token, state, revision, created_at, updated_at)
            VALUES ('attempt-active', ?, ?, ?, 1, 'offered', 1, ?, ?)
            """,
            (
                active_operation,
                machine_id,
                generation_number,
                history_size + 2.0,
                history_size + 2.0,
            ),
        )
        active_dispatch = {
            "operation_id": active_operation,
            "machine_id": machine_id,
            "edge_generation": edge_generation,
            "status": "offered",
            "created_at": history_size + 2.0,
        }
        connection.execute(
            """
            INSERT INTO entity_records
                (entity_type, entity_id, revision, record_json,
                 legacy_classification, source_import_id, created_at, updated_at)
            VALUES (?, ?, 1, ?, '', NULL, ?, ?)
            """,
            (
                EDGE_DISPATCH_ENTITY,
                active_operation,
                json.dumps(active_dispatch, separators=(",", ":")),
                history_size + 2.0,
                history_size + 2.0,
            ),
        )
        connection.execute(
            """
            INSERT INTO entity_control_index
                (entity_type, entity_id, machine_id, edge_generation, status,
                 sort_created_at)
            VALUES (?, ?, ?, ?, 'offered', ?)
            """,
            (
                EDGE_DISPATCH_ENTITY,
                active_operation,
                machine_id,
                edge_generation,
                history_size + 2.0,
            ),
        )

    decoded_rows = 0
    original_decoder = store._entity_from_row

    def counted_decoder(row):
        nonlocal decoded_rows
        decoded_rows += 1
        return original_decoder(row)

    store._entity_from_row = counted_decoder  # type: ignore[method-assign]
    started = time.process_time()
    receipts = edge_receipt_acknowledgements(
        store, machine_id, edge_generation, limit=5
    )
    dispatches = transport._dispatches_for_machine(
        machine_id, edge_generation, limit=5
    )
    elapsed = time.process_time() - started

    assert [item["receipt_id"] for item in receipts] == ["receipt-pending"]
    assert [item["entity_id"] for item in dispatches] == ["dispatch-active"]
    assert decoded_rows == 2
    assert elapsed < 1.0
    assert store.connection.execute(
        "SELECT COUNT(*) FROM entity_records WHERE entity_type = ?",
        (EDGE_RECEIPT_ENTITY,),
    ).fetchone()[0] == history_size + 1
    assert store.connection.execute(
        "SELECT COUNT(*) FROM entity_records WHERE entity_type = ?",
        (EDGE_DISPATCH_ENTITY,),
    ).fetchone()[0] == history_size + 1

    receipt_plan = " ".join(
        str(row[3])
        for row in store.connection.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT entity_id FROM entity_control_index
            WHERE entity_type = ? AND machine_id = ? AND edge_generation = ?
              AND status = 'pending'
            ORDER BY sort_created_at, entity_id LIMIT 5
            """,
            (EDGE_RECEIPT_ENTITY, machine_id, edge_generation),
        )
    )
    operation_plan = " ".join(
        str(row[3])
        for row in store.connection.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT operation_id FROM operations
            WHERE state IN ('dispatchable', 'running')
            ORDER BY created_at, operation_id LIMIT 5
            """
        )
    )
    assert "entity_control_route_status_idx" in receipt_plan
    assert "operations_state_created_idx" in operation_plan
    store.close()


def test_twenty_thousand_operation_group_associations_are_index_bounded(
    tmp_path, monkeypatch
) -> None:
    store = HubStoreV2(tmp_path / "operation-group-history.sqlite3")
    runtime = HubRuntimeV2(store)
    history_size = 20_000
    selected_operation_id = "operation-selected"
    principal_ref = store.principal_ref

    with store.immediate_transaction() as connection:
        operations = []
        associations = []
        association_index = []
        for index in range(history_size):
            operation_id = f"operation-history-{index:05d}"
            work_group_id = f"group-history-{index:05d}"
            created_at = float(index)
            operations.append(
                (
                    operation_id,
                    principal_ref,
                    "patchbay_worker_start",
                    "unrelated-target",
                    f"history-{index:05d}",
                    "0" * 64,
                    "created",
                    created_at,
                    created_at,
                )
            )
            associations.append(
                (
                    OPERATION_GROUP_ENTITY,
                    operation_id,
                    json.dumps(
                        {
                            "operation_id": operation_id,
                            "work_group_id": work_group_id,
                            "kind": "worker",
                        },
                        separators=(",", ":"),
                    ),
                    created_at,
                    created_at,
                )
            )
            association_index.append((operation_id, work_group_id, "worker"))

        selected_created_at = float(history_size + 1)
        operations.append(
            (
                selected_operation_id,
                principal_ref,
                "patchbay_worker_start",
                "unrelated-target",
                "selected-operation",
                "1" * 64,
                "created",
                selected_created_at,
                selected_created_at,
            )
        )
        associations.append(
            (
                OPERATION_GROUP_ENTITY,
                selected_operation_id,
                json.dumps(
                    {
                        "operation_id": selected_operation_id,
                        "work_group_id": "group-selected",
                        "kind": "worker",
                    },
                    separators=(",", ":"),
                ),
                selected_created_at,
                selected_created_at,
            )
        )
        association_index.append((selected_operation_id, "group-selected", "worker"))
        connection.executemany(
            """
            INSERT INTO operations
                (operation_id, principal_ref, tool, logical_target, idempotency_key,
                 semantic_payload_hash, state, revision, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            operations,
        )
        connection.executemany(
            """
            INSERT INTO entity_records
                (entity_type, entity_id, revision, record_json,
                 legacy_classification, source_import_id, created_at, updated_at)
            VALUES (?, ?, 1, ?, '', NULL, ?, ?)
            """,
            associations,
        )
        connection.executemany(
            """
            INSERT INTO operation_group_index(operation_id, work_group_id, kind)
            VALUES (?, ?, ?)
            """,
            association_index,
        )
        connection.execute(
            "UPDATE operations SET result_json = ? WHERE operation_id = ?",
            (json.dumps({"status": "pending"}), selected_operation_id),
        )

    decoded_rows = 0
    original_decoder = store_v2_module._decode_json_object

    def counted_decoder(raw, *, context):
        nonlocal decoded_rows
        decoded_rows += 1
        return original_decoder(raw, context=context)

    monkeypatch.setattr(store_v2_module, "_decode_json_object", counted_decoder)

    started = time.process_time()
    operations = runtime._operations_for_group("group-selected")
    elapsed = time.process_time() - started

    assert [operation["operation_id"] for operation in operations] == [
        selected_operation_id
    ]
    assert decoded_rows == 1
    assert elapsed < 1.0
    plan = " ".join(
        str(row[3])
        for row in store.connection.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT operation_id FROM operation_group_index
            WHERE work_group_id = ?
            ORDER BY operation_id
            """,
            ("group-selected",),
        )
    )
    assert "operation_group_index_group_operation_idx" in plan
    store.close()


def test_twenty_thousand_operation_group_status_is_bounded_and_paginated(
    tmp_path, monkeypatch
) -> None:
    store = HubStoreV2(tmp_path / "group-status-scale.sqlite3")
    runtime = HubRuntimeV2(store)
    group_id = "group-scale"
    created_at = time.time()
    store.put_entity(
        WORK_GROUP_ENTITY,
        group_id,
        {
            "work_group_id": group_id,
            "principal_ref": store.principal_ref,
            "title": "Scale status",
            "goal": "Keep manager status bounded.",
            "status": "open",
            "lifecycle": "open",
            "visibility": "private",
            "execution_mode": "end_to_end",
            "definition_of_done": "The status projection is bounded.",
            "lanes": {},
            "participants": [],
            "readiness": {},
            "created_at": created_at,
            "updated_at": created_at,
        },
        expected_revision=0,
    )
    operation_rows = []
    association_rows = []
    for index in range(20_000):
        operation_id = f"op_{index:05d}"
        state = (
            "running"
            if index == 19_997
            else ("reconciling" if index == 19_998 else "succeeded")
        )
        operation_rows.append(
            (
                operation_id,
                store.principal_ref,
                "patchbay_worker_status",
                group_id,
                f"group-status-{index:05d}",
                "0" * 64,
                state,
                1,
                created_at + index / 1_000,
                created_at + index / 1_000,
            )
        )
        association_rows.append((operation_id, group_id, "worker"))
    with store.immediate_transaction() as connection:
        connection.executemany(
            """
            INSERT INTO operations
                (operation_id, principal_ref, tool, logical_target,
                 idempotency_key, semantic_payload_hash, state, revision,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            operation_rows,
        )
        connection.executemany(
            """
            INSERT INTO operation_group_index(operation_id, work_group_id, kind)
            VALUES (?, ?, ?)
            """,
            association_rows,
        )

    decoded_rows = 0
    original_decoder = store_v2_module._decode_json_object

    def counted_decoder(raw, *, context):
        nonlocal decoded_rows
        decoded_rows += 1
        return original_decoder(raw, context=context)

    monkeypatch.setattr(store_v2_module, "_decode_json_object", counted_decoder)
    caller = RequestContext(
        client_ref="client-scale",
        chatgpt_session_ref="conversation-scale",
        work_run_ref="run-scale",
    )

    started = time.process_time()
    first = runtime.work_group_status(work_group_id=group_id, context=caller)
    elapsed = time.process_time() - started
    serialized = json.dumps(first, separators=(",", ":"))

    assert first["result"]["operation_summary"]["total"] == 20_000
    assert first["result"]["operation_summary"]["active"] == 1
    assert first["result"]["operation_summary"]["uncertain"] == 1
    assert len(first["result"]["operations"]) == 100
    assert first["result"]["operation_page"] == {
        "included": True,
        "total": 20_000,
        "cursor": "0",
        "limit": 100,
        "returned": 100,
        "next_cursor": "100",
        "truncated": True,
    }
    assert len(serialized) < 75_000
    assert decoded_rows <= 3
    assert elapsed < 1.0

    second = runtime.work_group_status(
        work_group_id=group_id,
        operation_cursor=first["result"]["operation_page"]["next_cursor"],
        context=caller,
    )
    assert second["result"]["operation_page"]["cursor"] == "100"
    assert {
        item["operation_id"] for item in first["result"]["operations"]
    }.isdisjoint(
        item["operation_id"] for item in second["result"]["operations"]
    )

    compact = runtime.work_group_status(
        work_group_id=group_id,
        include_operations=False,
        context=caller,
    )
    assert "operations" not in compact["result"]
    assert compact["result"]["operation_summary"]["total"] == 20_000
    assert compact["result"]["operation_page"]["included"] is False

    plan = " ".join(
        str(row[3])
        for row in store.connection.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT operation.operation_id
            FROM operation_group_index AS association
            JOIN operations AS operation
              ON operation.operation_id = association.operation_id
            WHERE association.work_group_id = ?
            """,
            (group_id,),
        )
    )
    assert "operation_group_index_group_operation_idx" in plan
    store.close()


@pytest.mark.asyncio
async def test_legacy_zero_child_batch_is_bounded_recovery_not_running(
    tmp_path,
) -> None:
    store = HubStoreV2(tmp_path / "legacy-empty-batch.sqlite3")

    async def must_not_wait(_seconds: float) -> None:
        raise AssertionError("recovery-required status must return without polling")

    broker = OperationBroker(store, sleep=must_not_wait)
    parent = broker.create_operation(
        tool="patchbay_worker_start_batch",
        logical_target="group-legacy",
        idempotency_key="legacy-empty",
        payload={"items": ["implementation", "verification"]},
    )
    parent = broker.prepare_operation(
        parent["operation_id"], expected_revision=parent["revision"]
    )
    parent = broker.make_dispatchable(
        parent["operation_id"], expected_revision=parent["revision"]
    )

    latest_revision = store.list_events()[-1]["event_revision"]
    status = await broker.operation_status(
        parent["operation_id"], wait_seconds=30, since_revision=latest_revision
    )

    assert status["status"] == "blocked"
    assert status["result"]["dispatch"]["state"] == "recovery_required"
    assert status["result"]["safe_next_action"] == "inspect_and_replace_batch"
    assert status["result"].get("children", []) == []
    assert status["next_actions"][0]["tool"] == "patchbay_operation_status"
    store.close()


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


@pytest.mark.asyncio
async def test_pull_offer_keeps_healthy_preclaim_operation_running(tmp_path) -> None:
    store = HubStoreV2(tmp_path / "healthy-preclaim.sqlite3")
    broker = OperationBroker(store)
    runtime = HubRuntimeV2(store, broker=broker)
    code = runtime.create_enrollment_code(name="Healthy Edge", tags=["codex"])[
        "code"
    ]
    enrolled = runtime.enroll_machine(
        code=code,
        machine_id="machine-healthy",
        edge_generation="generation-healthy",
        display_name="Healthy Edge",
        tags=["codex"],
    )
    runtime.heartbeat(
        machine_id="machine-healthy",
        token=enrolled["node_token"],
        edge_generation="generation-healthy",
        projection_revision=1,
        capabilities={
            "contract_hash": HUB_V2_CONTRACT_HASH,
            "action_capabilities": {"codex_worker_stop": "2"},
            "action_capability_versions": {"codex_worker_stop": "2"},
            "max_concurrent_jobs": 1,
            "queue_enabled": True,
        },
        workspaces=[],
        resource_status={"active_workers": 0, "free_worker_slots": 1},
    )
    transport = HubPullTransportBridgeV2(BoundServices(store, broker, runtime))
    payload = {
        "action": "codex_worker_stop",
        "arguments": {"worker": "Healthy Worker"},
        "machine_id": "machine-healthy",
        "edge_generation": "generation-healthy",
        "target": {
            "machine_id": "machine-healthy",
            "edge_generation": "generation-healthy",
        },
    }
    operation = _operation(
        broker, operation_id="op-healthy-preclaim", payload=payload
    )
    operation = broker.transition_operation(
        operation["operation_id"],
        expected_revision=int(operation["revision"]),
        state="running",
    )
    assert operation is not None

    pending = await transport.dispatch_operation(operation=operation, payload=payload)
    attempt_id = str(pending["result"]["attempt_id"])

    assert pending["status"] == "pending"
    assert pending["result"]["reason"] == "awaiting_edge_claim"
    assert pending["operation"]["state"] == "running"
    assert store.get_operation(operation["operation_id"])["state"] == "running"
    assert store.get_attempt(attempt_id)["state"] == "offered"
    generation = transport._generation_number("generation-healthy")
    assert edge_reconciliation_requests(
        store, "machine-healthy", generation
    ) == []
    store.close()


def test_single_slot_claim_interleaves_replay_with_new_offered_work(tmp_path) -> None:
    store = HubStoreV2(tmp_path / "fair-claim.sqlite3")
    broker = OperationBroker(store)
    runtime = HubRuntimeV2(store, broker=broker)
    code = runtime.create_enrollment_code(name="Fair Edge", tags=["codex"])["code"]
    enrolled = runtime.enroll_machine(
        code=code,
        machine_id="machine-fair",
        edge_generation="generation-fair",
        display_name="Fair Edge",
        tags=["codex"],
    )
    runtime.heartbeat(
        machine_id="machine-fair",
        token=enrolled["node_token"],
        edge_generation="generation-fair",
        projection_revision=1,
        capabilities={
            "contract_hash": HUB_V2_CONTRACT_HASH,
            "action_capabilities": {"codex_worker_stop": "2"},
            "action_capability_versions": {"codex_worker_stop": "2"},
            "max_concurrent_jobs": 1,
            "queue_enabled": True,
        },
        workspaces=[],
        resource_status={"active_workers": 0, "free_worker_slots": 1},
    )
    transport = HubPullTransportBridgeV2(BoundServices(store, broker, runtime))
    identity = {
        "machine_id": "machine-fair",
        "edge_generation": "generation-fair",
        "contract_hash": HUB_V2_CONTRACT_HASH,
        "available_slots": 1,
        "max_attempts": 1,
        "lease_seconds": 30,
    }

    def offer(operation_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        payload = {
            "action": "codex_worker_stop",
            "arguments": {"worker": operation_id},
            "machine_id": "machine-fair",
            "edge_generation": "generation-fair",
            "target": {
                "machine_id": "machine-fair",
                "edge_generation": "generation-fair",
            },
        }
        operation = _operation(broker, operation_id=operation_id, payload=payload)
        dispatch = transport._persist_dispatch(operation, payload)
        return operation, transport._offer_dispatch(operation, dispatch)

    old_offers: list[dict[str, Any]] = []
    # The claim selector intentionally reads a bounded candidate window. Put
    # fresh work beyond more than one minimum-size replay window so the test
    # proves that fairness is enforced across candidate classes, not merely
    # inside one oldest-first SQL page.
    for index in range(101):
        old_operation, old_offer = offer(f"op-old-replay-{index:03d}")
        broker.claim_attempt(
            old_operation["operation_id"],
            old_offer["attempt_id"],
            machine_id="machine-fair",
            edge_generation=transport._generation_number("generation-fair"),
            contract_hash=HUB_V2_CONTRACT_HASH,
            fencing_token=int(old_offer["fencing_token"]),
            lease_seconds=30,
            principal_ref=store.principal_ref,
        )
        old_offers.append(old_offer)
    new_operation, new_offer = offer("op-new-offered")

    repeated_old = transport.edge_claim(identity, token=enrolled["node_token"])["attempt"]
    newly_claimed = transport.edge_claim(identity, token=enrolled["node_token"])["attempt"]

    assert repeated_old is not None
    assert repeated_old["attempt_id"] == old_offers[0]["attempt_id"]
    assert repeated_old["idempotent_replay"] is True
    assert newly_claimed is not None
    assert newly_claimed["attempt_id"] == new_offer["attempt_id"]
    assert newly_claimed["idempotent_replay"] is False
    assert store.get_attempt(new_offer["attempt_id"])["state"] == "claimed"
    assert store.get_operation(new_operation["operation_id"])["state"] == "running"
    store.close()


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


def test_old_edge_contract_can_finish_fenced_attempt_during_rolling_upgrade(
    tmp_path,
) -> None:
    store = HubStoreV2(tmp_path / "rolling-upgrade.sqlite3")
    broker = OperationBroker(store)
    runtime = HubRuntimeV2(store, broker=broker)
    previous_contract = HUB_V2_CONTRACT_HASH
    current_contract = "next-edge-contract"
    code = runtime.create_enrollment_code(name="rolling-edge", tags=["codex"])["code"]
    enrolled = runtime.enroll_machine(
        code=code,
        machine_id="machine-old",
        edge_generation="generation-old",
        display_name="Rolling Edge",
        tags=["codex"],
    )
    capabilities = {
        "contract_hash": previous_contract,
        "action_capabilities": {"codex_worker_stop": "2"},
        "action_capability_versions": {"codex_worker_stop": "2"},
        "max_concurrent_jobs": 4,
        "queue_enabled": True,
    }
    runtime.heartbeat(
        machine_id="machine-old",
        token=enrolled["node_token"],
        edge_generation="generation-old",
        projection_revision=1,
        capabilities=capabilities,
        workspaces=[],
        resource_status={"active_workers": 0, "free_worker_slots": 4},
    )
    transport = HubPullTransportBridgeV2(BoundServices(store, broker, runtime))
    payload = {
        "action": "codex_worker_stop",
        "arguments": {"worker": "Rolling Worker"},
        "machine_id": "machine-old",
        "edge_generation": "generation-old",
        "target": {
            "machine_id": "machine-old",
            "edge_generation": "generation-old",
        },
    }
    operation = _operation(broker, operation_id="op-rolling-result", payload=payload)
    dispatch = transport._persist_dispatch(operation, payload)
    transport._offer_dispatch(operation, dispatch)
    claimed = transport.edge_claim(
        {
            "machine_id": "machine-old",
            "edge_generation": "generation-old",
            "contract_hash": previous_contract,
            "available_slots": 1,
            "max_attempts": 1,
            "lease_seconds": 30,
        },
        token=enrolled["node_token"],
    )["attempt"]
    executing = transport.edge_lease(
        {
            "machine_id": "machine-old",
            "edge_generation": "generation-old",
            "contract_hash": previous_contract,
            "operation_id": claimed["operation_id"],
            "attempt_id": claimed["attempt_id"],
            "fencing_token": claimed["fencing_token"],
            "expected_revision": claimed["revision"],
            "lease_seconds": 30,
        },
        token=enrolled["node_token"],
    )["attempt"]

    runtime.heartbeat(
        machine_id="machine-old",
        token=enrolled["node_token"],
        edge_generation="generation-old",
        projection_revision=2,
        capabilities={**capabilities, "contract_hash": current_contract},
        workspaces=[],
        resource_status={"active_workers": 0, "free_worker_slots": 4},
    )
    broker.expire_leases(now=float(executing["lease_expires_at"]) + 1)

    result = transport.edge_result(
        {
            "machine_id": "machine-old",
            "edge_generation": "generation-old",
            "contract_hash": current_contract,
            "session_contract_hash": current_contract,
            "contract": {
                "contract_hash": current_contract,
                "edge_generation": "generation-old",
            },
            "receipt": {
                "receipt_id": "receipt-rolling-result",
                "operation_id": claimed["operation_id"],
                "attempt_id": claimed["attempt_id"],
                "fencing_token": claimed["fencing_token"],
                "edge_generation": "generation-old",
                "contract_hash": previous_contract,
                "operation_payload_hash": dispatch["payload_hash"],
                "outcome": "succeeded",
                "result": {"accepted": True, "stopped": True},
                "error": "",
                "uncertain": False,
            },
        },
        token=enrolled["node_token"],
    )

    assert result["accepted"] is True
    assert store.get_operation(operation["operation_id"])["state"] == "succeeded"
    assert store.get_attempt(claimed["attempt_id"])["state"] == "acknowledged"
    receipts = store.list_entities(EDGE_RECEIPT_ENTITY)
    assert [item["entity_id"] for item in receipts] == ["receipt-rolling-result"]
    assert receipts[0]["record"]["contract_hash"] == previous_contract
    store.close()


def test_unclaimed_offer_rolls_to_current_contract_before_edge_claim(tmp_path) -> None:
    store = HubStoreV2(tmp_path / "unclaimed-contract-rollover.sqlite3")
    broker = OperationBroker(store)
    runtime = HubRuntimeV2(store, broker=broker)
    old_contract = HUB_V2_CONTRACT_HASH
    current_contract = "current-edge-contract"
    code = runtime.create_enrollment_code(name="rollover-edge", tags=["codex"])["code"]
    enrolled = runtime.enroll_machine(
        code=code,
        machine_id="machine-rollover",
        edge_generation="generation-rollover",
        display_name="Rollover Edge",
        tags=["codex"],
    )
    capabilities = {
        "contract_hash": old_contract,
        "action_capabilities": {"codex_worker_stop": "2"},
        "action_capability_versions": {"codex_worker_stop": "2"},
        "max_concurrent_jobs": 4,
        "queue_enabled": True,
    }
    runtime.heartbeat(
        machine_id="machine-rollover",
        token=enrolled["node_token"],
        edge_generation="generation-rollover",
        projection_revision=1,
        capabilities=capabilities,
        workspaces=[],
        resource_status={"active_workers": 0, "free_worker_slots": 4},
    )
    transport = HubPullTransportBridgeV2(BoundServices(store, broker, runtime))
    payload = {
        "action": "codex_worker_stop",
        "arguments": {"worker": "Rollover Worker"},
        "machine_id": "machine-rollover",
        "edge_generation": "generation-rollover",
        "target": {
            "machine_id": "machine-rollover",
            "edge_generation": "generation-rollover",
        },
    }
    operation = _operation(
        broker, operation_id="op-unclaimed-rollover", payload=payload
    )
    dispatch = transport._persist_dispatch(operation, payload)
    old_attempt = transport._offer_dispatch(operation, dispatch)

    runtime.heartbeat(
        machine_id="machine-rollover",
        token=enrolled["node_token"],
        edge_generation="generation-rollover",
        projection_revision=2,
        capabilities={**capabilities, "contract_hash": current_contract},
        workspaces=[],
        resource_status={"active_workers": 0, "free_worker_slots": 4},
    )
    claimed = transport.edge_claim(
        {
            "machine_id": "machine-rollover",
            "edge_generation": "generation-rollover",
            "contract_hash": current_contract,
            "available_slots": 1,
            "max_attempts": 1,
            "lease_seconds": 30,
        },
        token=enrolled["node_token"],
    )["attempt"]

    assert claimed is not None
    assert claimed["attempt_id"] != old_attempt["attempt_id"]
    assert claimed["required_contract_hash"] == current_contract
    assert store.get_attempt(old_attempt["attempt_id"])["state"] == "retryable"
    rows = store.connection.execute(
        "SELECT state FROM attempts WHERE operation_id = ? ORDER BY fencing_token",
        (operation["operation_id"],),
    ).fetchall()
    assert [str(row["state"]) for row in rows] == ["retryable", "claimed"]
    store.close()


def test_expired_initial_lease_successor_rolls_forward_across_another_contract_change(
    tmp_path,
) -> None:
    store = HubStoreV2(tmp_path / "retry-successor.sqlite3")
    broker = OperationBroker(store)
    runtime = HubRuntimeV2(store, broker=broker)
    previous_contract = HUB_V2_CONTRACT_HASH
    current_contract = "successor-edge-contract"
    code = runtime.create_enrollment_code(name="retry-edge", tags=["codex"])["code"]
    enrolled = runtime.enroll_machine(
        code=code,
        machine_id="machine-retry",
        edge_generation="generation-retry",
        display_name="Retry Edge",
        tags=["codex"],
    )
    capabilities = {
        "contract_hash": previous_contract,
        "action_capabilities": {"codex_worker_stop": "2"},
        "action_capability_versions": {"codex_worker_stop": "2"},
        "max_concurrent_jobs": 4,
        "queue_enabled": True,
    }
    runtime.heartbeat(
        machine_id="machine-retry",
        token=enrolled["node_token"],
        edge_generation="generation-retry",
        projection_revision=1,
        capabilities=capabilities,
        workspaces=[],
        resource_status={"active_workers": 0, "free_worker_slots": 4},
    )
    transport = HubPullTransportBridgeV2(BoundServices(store, broker, runtime))
    payload = {
        "action": "codex_worker_stop",
        "arguments": {"worker": "Retry Worker"},
        "machine_id": "machine-retry",
        "edge_generation": "generation-retry",
        "target": {
            "machine_id": "machine-retry",
            "edge_generation": "generation-retry",
        },
    }
    operation = _operation(broker, operation_id="op-retry-successor", payload=payload)
    dispatch = transport._persist_dispatch(operation, payload)
    transport._offer_dispatch(operation, dispatch)
    claimed = transport.edge_claim(
        {
            "machine_id": "machine-retry",
            "edge_generation": "generation-retry",
            "contract_hash": previous_contract,
            "available_slots": 1,
            "max_attempts": 1,
            "lease_seconds": 1,
        },
        token=enrolled["node_token"],
    )["attempt"]
    broker.expire_leases(now=float(claimed["lease_expires_at"]) + 1)
    runtime.heartbeat(
        machine_id="machine-retry",
        token=enrolled["node_token"],
        edge_generation="generation-retry",
        projection_revision=2,
        capabilities={**capabilities, "contract_hash": current_contract},
        workspaces=[],
        resource_status={"active_workers": 0, "free_worker_slots": 4},
    )
    recovery = {
        "machine_id": "machine-retry",
        "edge_generation": "generation-retry",
        "session_contract_hash": current_contract,
        "contract": {
            "contract_hash": current_contract,
            "edge_generation": "generation-retry",
        },
        "contract_hash": previous_contract,
        "operation_id": claimed["operation_id"],
        "attempt_id": claimed["attempt_id"],
        "fencing_token": claimed["fencing_token"],
        "local_recovery": {
            "recovery_action": "lease_reconciliation",
            "found": False,
            "effect_started": False,
            "reason": "lease_not_confirmed",
        },
    }

    first = transport.edge_reconcile(recovery, token=enrolled["node_token"])
    next_contract = "next-successor-edge-contract"
    runtime.heartbeat(
        machine_id="machine-retry",
        token=enrolled["node_token"],
        edge_generation="generation-retry",
        projection_revision=3,
        capabilities={**capabilities, "contract_hash": next_contract},
        workspaces=[],
        resource_status={"active_workers": 0, "free_worker_slots": 4},
    )
    recovery["session_contract_hash"] = next_contract
    recovery["contract"]["contract_hash"] = next_contract
    second = transport.edge_reconcile(recovery, token=enrolled["node_token"])
    third = transport.edge_reconcile(recovery, token=enrolled["node_token"])

    first_retry = first["retry_attempts"][0]
    second_retry = second["retry_attempts"][0]
    third_retry = third["retry_attempts"][0]
    assert first_retry["attempt_id"] != second_retry["attempt_id"]
    assert second_retry["attempt_id"] == third_retry["attempt_id"]
    assert first_retry["required_contract_hash"] == current_contract
    assert second_retry["required_contract_hash"] == next_contract
    assert store.get_attempt(claimed["attempt_id"])["state"] == "retryable"
    assert store.get_attempt(first_retry["attempt_id"])["state"] == "retryable"
    rows = store.connection.execute(
        "SELECT attempt_id, state FROM attempts WHERE operation_id = ? ORDER BY fencing_token",
        (operation["operation_id"],),
    ).fetchall()
    assert [str(row["attempt_id"]) for row in rows] == [
        claimed["attempt_id"],
        first_retry["attempt_id"],
        second_retry["attempt_id"],
    ]
    assert [str(row["state"]) for row in rows] == [
        "retryable",
        "retryable",
        "offered",
    ]
    store.close()


def test_durable_result_reconciles_an_expired_attempt_before_finishing(
    tmp_path,
) -> None:
    store = HubStoreV2(tmp_path / "expired-result.sqlite3")
    broker = OperationBroker(store)
    runtime = HubRuntimeV2(store, broker=broker)
    code = runtime.create_enrollment_code(name="edge-result", tags=["codex"])["code"]
    enrolled = runtime.enroll_machine(
        code=code,
        machine_id="machine-result",
        edge_generation="edgegen-result",
        display_name="Result Edge",
        tags=["codex"],
    )
    runtime.heartbeat(
        machine_id="machine-result",
        token=enrolled["node_token"],
        edge_generation="edgegen-result",
        projection_revision=1,
        capabilities={
            "contract_hash": HUB_V2_CONTRACT_HASH,
            "action_capabilities": {"codex_worker_stop": "2"},
            "action_capability_versions": {"codex_worker_stop": "2"},
            "max_concurrent_jobs": 4,
            "queue_enabled": True,
        },
        workspaces=[],
        resource_status={"active_workers": 0, "free_worker_slots": 4},
    )
    transport = HubPullTransportBridgeV2(BoundServices(store, broker, runtime))
    payload = {
        "action": "codex_worker_stop",
        "arguments": {"worker": "Completed Worker"},
        "machine_id": "machine-result",
        "edge_generation": "edgegen-result",
        "target": {
            "machine_id": "machine-result",
            "edge_generation": "edgegen-result",
        },
    }
    operation = _operation(
        broker,
        operation_id="op-expired-result",
        payload=payload,
    )
    dispatch = transport._persist_dispatch(operation, payload)
    transport._offer_dispatch(operation, dispatch)
    identity = {
        "machine_id": "machine-result",
        "edge_generation": "edgegen-result",
        "contract_hash": HUB_V2_CONTRACT_HASH,
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
    broker.expire_leases(now=float(executing["lease_expires_at"]) + 1)

    result = transport.edge_result(
        {
            **identity,
            "receipt": {
                "receipt_id": "receipt-expired-result",
                "operation_id": claimed["operation_id"],
                "attempt_id": claimed["attempt_id"],
                "fencing_token": claimed["fencing_token"],
                "edge_generation": "edgegen-result",
                "contract_hash": HUB_V2_CONTRACT_HASH,
                "operation_payload_hash": dispatch["payload_hash"],
                "outcome": "succeeded",
                "result": {"accepted": True, "stopped": True},
                "error": "",
                "uncertain": False,
            },
        },
        token=enrolled["node_token"],
    )

    assert result["accepted"] is True
    assert store.get_operation("op-expired-result")["state"] == "succeeded"
    assert store.get_attempt(claimed["attempt_id"])["state"] == "acknowledged"
    store.close()


@pytest.mark.asyncio
async def test_terminal_dispatch_history_cannot_starve_new_work(tmp_path) -> None:
    store = HubStoreV2(tmp_path / "dispatch-history.sqlite3")
    broker = OperationBroker(store)
    runtime = HubRuntimeV2(store, broker=broker)
    edge = RecordingEdge()
    port = HubBrokerEdgeDispatchPortV2(broker, runtime, EdgeDeliveryBridgeV2(edge))
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

    terminal_revisions = {
        item["entity_id"]: item["revision"]
        for item in store.list_entities(EDGE_DISPATCH_ENTITY)
    }

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
    assert {
        item["entity_id"]: item["revision"]
        for item in store.list_entities(EDGE_DISPATCH_ENTITY)
        if item["entity_id"] in terminal_revisions
    } == terminal_revisions
    store.close()


@pytest.mark.asyncio
async def test_reopened_batch_dispatch_advances_children_left_created(tmp_path) -> None:
    database_path = tmp_path / "batch-crash.sqlite3"
    store = HubStoreV2(database_path)
    broker = OperationBroker(store)
    edge = RecordingEdge()
    port = HubBrokerEdgeDispatchPortV2(
        broker, HubRuntimeV2(store, broker=broker), EdgeDeliveryBridgeV2(edge)
    )
    child_specs = [
        {
            "item_id": item_id,
            "tool": "patchbay_worker_start",
            "logical_target": f"group-crash/{item_id}",
            "payload": {
                "action": "codex_batch_probe",
                "arguments": {"name": item_id.title(), "brief": f"Private {item_id}"},
            },
        }
        for item_id in ("reader", "writer")
    ]
    batch = port.create_batch_operation(
        logical_target="group-crash",
        idempotency_key="batch-crash-1",
        payload={"action": "compound.codex_worker_start"},
        child_specs=child_specs,
    )
    child_ids = [child["operation_id"] for child in batch["children"]]

    assert [child["state"] for child in batch["children"]] == ["created", "created"]
    assert {
        entity["entity_id"] for entity in store.list_entities(EDGE_DISPATCH_ENTITY)
    } == set(child_ids)
    manifest = store.get_entity(
        "hub.operation_batch_child_manifest", batch["parent"]["operation_id"]
    )["record"]
    assert "payload" not in str(manifest)
    assert "Private reader" not in str(manifest)
    store.close()

    reopened_store = HubStoreV2(database_path)
    reopened_broker = OperationBroker(reopened_store)
    reopened_port = HubBrokerEdgeDispatchPortV2(
        reopened_broker,
        HubRuntimeV2(reopened_store, broker=reopened_broker),
        EdgeDeliveryBridgeV2(edge),
    )

    delivered = await reopened_port.dispatch_pending(max_operations=10)

    assert delivered == child_ids
    assert [call["arguments"]["name"] for call in edge.calls] == ["Reader", "Writer"]
    assert [reopened_store.get_operation(child_id)["state"] for child_id in child_ids] == [
        "succeeded",
        "succeeded",
    ]
    assert reopened_store.get_operation(batch["parent"]["operation_id"])["state"] == "succeeded"
    reopened_store.close()


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
    assert (
        store.get_entity(EDGE_RECEIPT_ENTITY, "receipt-000")["record"]["status"]
        == "retired"
    )

    second_page = transport._control_response({}, identity)["receipt_acknowledgements"]
    assert [item["receipt_id"] for item in second_page] == ["receipt-100"]
    assert (
        transport.edge_outbox_ack(
            {**identity, "receipt_ids": ["receipt-100"]}, token="ignored"
        )["accepted"]
        is True
    )
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
    port = HubBrokerEdgeDispatchPortV2(broker, runtime, EdgeDeliveryBridgeV2(edge))
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
