from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from patchbay.hub.broker import OperationBroker
from patchbay.hub.runtime_v2 import WORK_GROUP_ENTITY, HubRuntimeV2
from patchbay.hub.store_v2 import (
    HubStoreV2,
    HubStoreV2Conflict,
    HubStoreV2StateError,
    semantic_payload_hash,
)


CONTRACT_HASH = "contract-v2-sha256"


def _create_dispatchable(
    broker: OperationBroker,
    *,
    key: str = "retry-key",
    target: str = "group-a",
    payload: dict | None = None,
) -> dict:
    operation = broker.create_operation(
        tool="patchbay_worker_start",
        logical_target=target,
        idempotency_key=key,
        payload=payload or {"name": "Reader"},
    )
    operation = broker.prepare_operation(
        operation["operation_id"], expected_revision=operation["revision"]
    )
    return broker.make_dispatchable(
        operation["operation_id"], expected_revision=operation["revision"]
    )


def _offer_claim_execute(
    broker: OperationBroker,
    operation: dict,
    *,
    machine_id: str = "edge-a",
    generation: int = 4,
    lease_seconds: float = 30,
) -> dict:
    attempt = broker.offer_attempt(
        operation["operation_id"],
        machine_id=machine_id,
        edge_generation=generation,
        required_contract_hash=CONTRACT_HASH,
    )
    attempt = broker.claim_attempt(
        operation["operation_id"],
        attempt["attempt_id"],
        machine_id=machine_id,
        edge_generation=generation,
        contract_hash=CONTRACT_HASH,
        fencing_token=attempt["fencing_token"],
        lease_seconds=lease_seconds,
    )
    return broker.mark_attempt_executing(
        operation["operation_id"],
        attempt["attempt_id"],
        expected_revision=attempt["revision"],
        machine_id=machine_id,
        edge_generation=generation,
        contract_hash=CONTRACT_HASH,
        fencing_token=attempt["fencing_token"],
    )


def _finish(
    broker: OperationBroker,
    store: HubStoreV2,
    operation_id: str,
    attempt: dict,
    result: dict,
) -> dict | None:
    operation = store.get_operation(operation_id)
    return broker.finish_operation(
        operation_id,
        attempt["attempt_id"],
        expected_revision=operation["revision"],
        expected_attempt_revision=attempt["revision"],
        machine_id=attempt["machine_id"],
        edge_generation=attempt["edge_generation"],
        contract_hash=CONTRACT_HASH,
        fencing_token=attempt["fencing_token"],
        result=result,
    )


def test_unclaimed_offer_and_claim_are_ordinary_active_execution(tmp_path):
    store = HubStoreV2(tmp_path / "healthy-claim.sqlite3")
    broker = OperationBroker(store)
    operation = _create_dispatchable(broker, key="healthy-claim")

    offered = broker.offer_attempt(
        operation["operation_id"],
        machine_id="edge-a",
        edge_generation=4,
        required_contract_hash=CONTRACT_HASH,
    )

    assert offered["state"] == "offered"
    assert store.get_operation(operation["operation_id"])["state"] == "dispatchable"

    claimed = broker.claim_attempt(
        operation["operation_id"],
        offered["attempt_id"],
        machine_id="edge-a",
        edge_generation=4,
        contract_hash=CONTRACT_HASH,
        fencing_token=offered["fencing_token"],
    )
    executing = broker.mark_attempt_executing(
        operation["operation_id"],
        claimed["attempt_id"],
        expected_revision=claimed["revision"],
        machine_id="edge-a",
        edge_generation=4,
        contract_hash=CONTRACT_HASH,
        fencing_token=claimed["fencing_token"],
    )

    assert executing["state"] == "executing"
    assert store.get_operation(operation["operation_id"])["state"] == "running"
    store.close()


def test_create_requires_stable_idempotency_and_rejects_semantic_conflict(tmp_path):
    store = HubStoreV2(tmp_path / "hub.sqlite3")
    broker = OperationBroker(store)

    with pytest.raises(ValueError, match="idempotency_key"):
        broker.create_operation(
            tool="patchbay_worker_start",
            logical_target="group-a",
            idempotency_key="",
            payload={"name": "Reader"},
        )

    first = broker.create_operation(
        tool="patchbay_worker_start",
        logical_target="group-a",
        idempotency_key="stable-key",
        payload={"name": "Reader", "options": {"b": 2, "a": 1}},
    )
    replay = broker.create_operation(
        tool="patchbay_worker_start",
        logical_target="group-a",
        idempotency_key="stable-key",
        payload={"options": {"a": 1, "b": 2}, "name": "Reader"},
    )

    assert replay["operation_id"] == first["operation_id"]
    assert replay["idempotent_replay"] is True
    with pytest.raises(HubStoreV2Conflict, match="idempotency_payload_conflict"):
        broker.create_operation(
            tool="patchbay_worker_start",
            logical_target="group-a",
            idempotency_key="stable-key",
            payload={"name": "Writer"},
        )
    assert (
        store.connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
    )


def test_operation_group_association_is_idempotent_and_conflict_checked(tmp_path):
    store = HubStoreV2(tmp_path / "hub.sqlite3")
    broker = OperationBroker(store)
    operation = broker.create_operation(
        tool="patchbay_worker_start",
        logical_target="worker-target",
        idempotency_key="group-association",
        payload={"name": "Worker"},
    )

    first = broker.associate_operation(
        operation["operation_id"], work_group_id="group-alpha"
    )
    replay = broker.associate_operation(
        operation["operation_id"], work_group_id="group-alpha"
    )

    assert first["entity_id"] == replay["entity_id"] == operation["operation_id"]
    assert replay["record"]["work_group_id"] == "group-alpha"
    with pytest.raises(HubStoreV2Conflict, match="operation_work_group_conflict"):
        broker.associate_operation(
            operation["operation_id"], work_group_id="group-beta"
        )


def test_child_identity_is_stable_and_parent_aggregates_mixed_results(tmp_path):
    store = HubStoreV2(tmp_path / "hub.sqlite3")
    broker = OperationBroker(store)
    batch = broker.create_batch_operation(
        logical_target="group-a",
        idempotency_key="batch-key",
        payload={"items": ["reader", "writer"]},
        child_specs=[
            {
                "item_id": "reader",
                "tool": "patchbay_worker_start",
                "logical_target": "group-a/reader",
                "payload": {"name": "Reader"},
            },
            {
                "item_id": "writer",
                "tool": "patchbay_worker_start",
                "logical_target": "group-a/writer",
                "payload": {"name": "Writer"},
            },
        ],
    )
    parent = batch["parent"]
    reader, writer = batch["children"]
    reader_replay = broker.create_child_operation(
        parent["operation_id"],
        item_id="reader",
        tool="patchbay_worker_start",
        logical_target="group-a/reader",
        payload={"name": "Reader"},
    )
    assert (
        store.get_entity("hub.operation_batch_child_manifest", parent["operation_id"])[
            "record"
        ]["version"]
        == 3
    )

    assert reader_replay["operation_id"] == reader["operation_id"]
    assert reader_replay["idempotency_key"] == OperationBroker.child_idempotency_key(
        parent["operation_id"], "reader"
    )
    with pytest.raises(HubStoreV2Conflict, match="child_operation_payload_conflict"):
        broker.create_child_operation(
            parent["operation_id"],
            item_id="reader",
            tool="patchbay_worker_start",
            logical_target="group-a/reader",
            payload={"name": "Changed"},
        )

    for operation, result in (
        (reader, {"accepted": True, "worker_id": "worker-reader"}),
        (writer, {"accepted": False, "reason": "capacity_blocked"}),
    ):
        operation = broker.prepare_operation(
            operation["operation_id"], expected_revision=operation["revision"]
        )
        operation = broker.make_dispatchable(
            operation["operation_id"], expected_revision=operation["revision"]
        )
        attempt = _offer_claim_execute(broker, operation)
        _finish(broker, store, operation["operation_id"], attempt, result)

    parent = store.get_operation(parent["operation_id"])
    assert parent["state"] == "succeeded"
    assert parent["result"]["status"] == "partial"
    assert [item["status"] for item in parent["result"]["result"]["items"]] == [
        "ok",
        "blocked",
    ]
    assert len(broker.list_child_operations(parent["operation_id"])) == 2


@pytest.mark.asyncio
async def test_manifestless_legacy_parent_becomes_terminal_when_children_finish(
    tmp_path,
):
    store = HubStoreV2(tmp_path / "legacy-terminal-children.sqlite3")
    broker = OperationBroker(store)
    parent = broker.create_operation(
        tool="patchbay_worker_start_batch",
        logical_target="group-legacy",
        idempotency_key="legacy-without-manifest",
        payload={"items": ["reader", "writer"]},
    )
    children = [
        broker.create_child_operation(
            parent["operation_id"],
            item_id=item_id,
            tool="patchbay_worker_start",
            logical_target=f"group-legacy/{item_id}",
            payload={"name": item_id.title()},
        )
        for item_id in ("reader", "writer")
    ]

    for index, child in enumerate(children):
        child = broker.prepare_operation(
            child["operation_id"], expected_revision=child["revision"]
        )
        child = broker.make_dispatchable(
            child["operation_id"], expected_revision=child["revision"]
        )
        child = broker.transition_operation(
            child["operation_id"],
            expected_revision=child["revision"],
            state="running",
        )
        broker.transition_operation(
            child["operation_id"],
            expected_revision=child["revision"],
            state="blocked",
            result={"status": "blocked", "result": {"reason": "test_terminal"}},
        )
        expected_parent_state = "created" if index == 0 else "blocked"
        assert (
            store.get_operation(parent["operation_id"])["state"]
            == expected_parent_state
        )

    reconciled = store.get_operation(parent["operation_id"])
    assert reconciled["result"]["status"] == "blocked"
    assert reconciled["result"]["result"] == {
        "reason": "legacy_batch_manifest_missing",
        "legacy_compatibility_reconciliation": True,
        "completion_claimed": False,
        "observed_child_count": 2,
        "observed_children": [
            {
                "item_id": "reader",
                "operation_id": children[0]["operation_id"],
                "state": "blocked",
                "status": "blocked",
            },
            {
                "item_id": "writer",
                "operation_id": children[1]["operation_id"],
                "state": "blocked",
                "status": "blocked",
            },
        ],
    }
    status = await broker.operation_status(parent["operation_id"])
    assert status["status"] == "blocked"
    assert status["result"]["outcome"]["terminal"] is True
    assert status["warnings"][0]["details"] == {
        "reason": "missing_atomic_child_manifest",
        "actual_child_count": 2,
    }
    assert (
        store.connection.execute(
            """
            SELECT COUNT(*) FROM events
            WHERE operation_id = ?
              AND event_type = 'operation.legacy_batch_parent_terminalized'
            """,
            (parent["operation_id"],),
        ).fetchone()[0]
        == 1
    )


def test_group_status_retires_only_its_manifestless_parent_with_terminal_children(
    tmp_path,
):
    store = HubStoreV2(tmp_path / "legacy-startup-reconciliation.sqlite3")
    broker = OperationBroker(store)
    terminal_parent = broker.create_operation(
        tool="patchbay_worker_start_batch",
        logical_target="group-terminal",
        idempotency_key="legacy-terminal-parent",
        payload={"items": ["reader"]},
    )
    terminal_child = broker.create_child_operation(
        terminal_parent["operation_id"],
        item_id="reader",
        tool="patchbay_worker_start",
        logical_target="group-terminal/reader",
        payload={"name": "Reader"},
    )
    active_parent = broker.create_operation(
        tool="patchbay_worker_start_batch",
        logical_target="group-active",
        idempotency_key="legacy-active-parent",
        payload={"items": ["reader"]},
    )
    broker.create_child_operation(
        active_parent["operation_id"],
        item_id="reader",
        tool="patchbay_worker_start",
        logical_target="group-active/reader",
        payload={"name": "Reader"},
    )
    with store.immediate_transaction() as connection:
        connection.execute(
            """
            UPDATE operations
            SET state = 'blocked', revision = 6, result_json = ?, updated_at = updated_at + 1
            WHERE operation_id = ?
            """,
            (
                json.dumps(
                    {"status": "blocked", "result": {"reason": "legacy_terminal"}}
                ),
                terminal_child["operation_id"],
            ),
        )
    store.put_entity(
        WORK_GROUP_ENTITY,
        "group-terminal",
        {
            "work_group_id": "group-terminal",
            "principal_ref": store.principal_ref,
            "title": "Legacy terminal group",
            "goal": "Prove bounded compatibility reconciliation.",
            "status": "open",
            "lifecycle": "active",
            "visibility": "private",
            "execution_mode": "end_to_end",
            "definition_of_done": "Retire only the selected legacy parent.",
            "lanes": {},
            "created_at": 1.0,
            "updated_at": 1.0,
        },
        expected_revision=0,
    )

    runtime = HubRuntimeV2(store, broker=broker)
    status = runtime.work_group_status(work_group_id="group-terminal")

    assert (
        status["result"]["completion_contract"]["activity_counts"][
            "active_operations"
        ]
        == 0
    )
    assert store.get_operation(terminal_parent["operation_id"])["state"] == "blocked"
    assert store.get_operation(active_parent["operation_id"])["state"] == "created"
    assert (
        store.work_group_status_projection("group-terminal")["operation_summary"][
            "active"
        ]
        == 0
    )
    assert (
        store.work_group_status_projection("group-active")["operation_summary"][
            "active"
        ]
        == 1
    )
    assert broker.reconcile_manifestless_terminal_batch_parents(
        operation_ids=[terminal_parent["operation_id"]]
    ) == {
        "repaired": [],
        "repaired_count": 0,
    }


def test_batch_manifest_is_immutable_and_rejects_undeclared_children(tmp_path):
    store = HubStoreV2(tmp_path / "hub.sqlite3")
    broker = OperationBroker(store)
    parent = broker.create_operation(
        tool="patchbay_worker_start_batch",
        logical_target="group-a",
        idempotency_key="manifest-key",
        payload={"items": ["reader", "writer"]},
    )

    manifest = broker.declare_child_manifest(
        parent["operation_id"], expected_item_ids=["reader", "writer"]
    )
    replay = broker.declare_child_manifest(
        parent["operation_id"], expected_item_ids=["reader", "writer"]
    )

    assert manifest["idempotent_replay"] is False
    assert replay["idempotent_replay"] is True
    assert replay["record"]["expected_item_ids"] == ["reader", "writer"]
    with pytest.raises(HubStoreV2Conflict, match="batch_child_manifest_conflict"):
        broker.declare_child_manifest(
            parent["operation_id"], expected_item_ids=["reader", "reviewer"]
        )
    with pytest.raises(
        HubStoreV2Conflict, match="child_operation_not_declared_in_manifest"
    ):
        broker.create_child_operation(
            parent["operation_id"],
            item_id="reviewer",
            tool="patchbay_worker_start",
            logical_target="group-a/reviewer",
            payload={"name": "Reviewer"},
        )


def test_atomic_batch_rolls_back_parent_manifest_and_every_child_on_failure(
    tmp_path, monkeypatch
):
    store = HubStoreV2(tmp_path / "hub.sqlite3")
    broker = OperationBroker(store)
    append_event = store._append_event_in_transaction
    event_count = 0
    child_specs = [
        {
            "item_id": item_id,
            "tool": "patchbay_worker_start",
            "logical_target": f"group-a/{item_id}",
            "payload": {
                "arguments": {
                    "name": item_id.title(),
                    "brief": f"Private rollback brief for {item_id}",
                    "repo_path": "/private/rollback/repo",
                },
                "context": {"machine_id": "private-rollback-machine"},
            },
        }
        for item_id in ("reader", "writer")
    ]

    def fail_during_second_child(connection, event_type, data, **kwargs):
        nonlocal event_count
        event_count += 1
        if event_count == 4:
            raise RuntimeError("simulated process boundary")
        return append_event(connection, event_type, data, **kwargs)

    monkeypatch.setattr(store, "_append_event_in_transaction", fail_during_second_child)

    with pytest.raises(RuntimeError, match="simulated process boundary"):
        broker.create_batch_operation(
            logical_target="group-a",
            idempotency_key="atomic-failure",
            payload={"items": ["reader", "writer"]},
            child_specs=child_specs,
        )

    assert store.connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
    assert store.list_entities("hub.operation_batch_child_manifest") == []
    assert store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0

    monkeypatch.setattr(store, "_append_event_in_transaction", append_event)
    recovered = broker.create_batch_operation(
        logical_target="group-a",
        idempotency_key="atomic-failure",
        payload={"items": ["reader", "writer"]},
        child_specs=child_specs,
    )
    durable_manifest = store.get_entity(
        "hub.operation_batch_child_manifest", recovered["parent"]["operation_id"]
    )["record"]
    assert set(durable_manifest) == {
        "version",
        "operation_id",
        "expected_item_ids",
        "expected_child_count",
        "child_hashes",
        "manifest_hash",
    }
    serialized_manifest = str(durable_manifest).lower()
    for forbidden in (
        "payload",
        "brief",
        "arguments",
        "context",
        "repo_path",
        "private-rollback-machine",
        "/private/rollback/repo",
    ):
        assert forbidden not in serialized_manifest


def test_atomic_batch_replay_is_idempotent_and_rejects_payload_conflicts(tmp_path):
    store = HubStoreV2(tmp_path / "hub.sqlite3")
    broker = OperationBroker(store)
    child_specs = [
        {
            "item_id": item_id,
            "tool": "patchbay_worker_start",
            "logical_target": f"group-a/{item_id}",
            "payload": {
                "action": "codex_worker_start",
                "arguments": {
                    "name": item_id.title(),
                    "brief": f"Private natural-language brief for {item_id}",
                    "repo_path": "/private/workspaces/patchbay",
                },
                "context": {"work_group_id": "group-a", "lane_id": item_id},
                "target": {"machine_id": "machine-private"},
            },
        }
        for item_id in ("reader", "writer")
    ]

    first = broker.create_batch_operation(
        logical_target="group-a",
        idempotency_key="atomic-replay",
        payload={"items": ["reader", "writer"]},
        child_specs=child_specs,
    )
    replay = broker.create_batch_operation(
        logical_target="group-a",
        idempotency_key="atomic-replay",
        payload={"items": ["reader", "writer"]},
        child_specs=child_specs,
    )

    assert replay["idempotent_replay"] is True
    assert replay["parent"]["operation_id"] == first["parent"]["operation_id"]
    assert [child["operation_id"] for child in replay["children"]] == [
        child["operation_id"] for child in first["children"]
    ]
    child_hashes = [
        {
            "item_id": spec["item_id"],
            "semantic_hash": semantic_payload_hash(spec["payload"]),
        }
        for spec in child_specs
    ]
    expected_item_ids = [spec["item_id"] for spec in child_specs]
    durable_manifest = store.get_entity(
        "hub.operation_batch_child_manifest", first["parent"]["operation_id"]
    )["record"]
    assert durable_manifest == {
        "version": 3,
        "operation_id": first["parent"]["operation_id"],
        "expected_item_ids": expected_item_ids,
        "expected_child_count": 2,
        "child_hashes": child_hashes,
        "manifest_hash": semantic_payload_hash(
            {
                "expected_item_ids": expected_item_ids,
                "child_hashes": child_hashes,
            }
        ),
    }
    serialized_manifest = str(durable_manifest).lower()
    for forbidden in (
        "payload",
        "brief",
        "arguments",
        "context",
        "repo_path",
        "machine-private",
        "/private/workspaces/patchbay",
    ):
        assert forbidden not in serialized_manifest
    assert (
        store.connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 3
    )
    with pytest.raises(HubStoreV2Conflict, match="idempotency_payload_conflict"):
        broker.create_batch_operation(
            logical_target="group-a",
            idempotency_key="atomic-replay",
            payload={"items": ["reader", "reviewer"]},
            child_specs=child_specs,
        )
    conflicting_specs = [dict(spec) for spec in child_specs]
    conflicting_specs[1] = {
        **conflicting_specs[1],
        "payload": {"name": "Changed Writer"},
    }
    with pytest.raises(HubStoreV2Conflict, match="batch_child_manifest_conflict"):
        broker.create_batch_operation(
            logical_target="group-a",
            idempotency_key="atomic-replay",
            payload={"items": ["reader", "writer"]},
            child_specs=conflicting_specs,
        )


def test_atomic_batch_includes_dispatch_records_and_rolls_everything_back(
    tmp_path, monkeypatch
):
    store = HubStoreV2(tmp_path / "hub.sqlite3")
    broker = OperationBroker(store)
    child_specs = [
        {
            "item_id": item_id,
            "tool": "patchbay_worker_start",
            "logical_target": f"group-a/{item_id}",
            "payload": {
                "action": "codex_worker_start",
                "arguments": {"name": item_id.title(), "brief": f"Private {item_id}"},
            },
        }
        for item_id in ("reader", "writer")
    ]
    dispatch_specs = [
        {
            "item_id": spec["item_id"],
            "action": spec["payload"]["action"],
            "payload": spec["payload"],
        }
        for spec in child_specs
    ]
    put_entity = store._put_entity_in_transaction
    dispatch_count = 0

    def fail_second_dispatch(connection, entity_type, entity_id, record, **kwargs):
        nonlocal dispatch_count
        if entity_type == "hub.edge_dispatch":
            dispatch_count += 1
            if dispatch_count == 2:
                raise RuntimeError("simulated dispatch persistence crash")
        return put_entity(connection, entity_type, entity_id, record, **kwargs)

    monkeypatch.setattr(store, "_put_entity_in_transaction", fail_second_dispatch)
    with pytest.raises(RuntimeError, match="simulated dispatch persistence crash"):
        broker.create_batch_operation(
            logical_target="group-a",
            idempotency_key="dispatch-rollback",
            payload={"items": ["reader", "writer"]},
            child_specs=child_specs,
            child_dispatch_specs=dispatch_specs,
        )

    assert (
        store.connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
    )
    assert store.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert store.list_entities("hub.operation_batch_child_manifest") == []
    assert store.list_entities("hub.edge_dispatch") == []


def test_atomic_batch_dispatch_replay_verifies_private_payload_hashes(tmp_path):
    store = HubStoreV2(tmp_path / "hub.sqlite3")
    broker = OperationBroker(store)
    child_specs = [
        {
            "item_id": "reader",
            "tool": "patchbay_worker_start",
            "logical_target": "group-a/reader",
            "payload": {
                "action": "codex_worker_start",
                "arguments": {"name": "Reader", "brief": "Private reader brief"},
            },
        }
    ]
    dispatch_specs = [
        {
            "item_id": "reader",
            "action": "codex_worker_start",
            "payload": child_specs[0]["payload"],
        }
    ]
    first = broker.create_batch_operation(
        logical_target="group-a",
        idempotency_key="dispatch-replay",
        payload={"items": ["reader"]},
        child_specs=child_specs,
        child_dispatch_specs=dispatch_specs,
    )
    replay = broker.create_batch_operation(
        logical_target="group-a",
        idempotency_key="dispatch-replay",
        payload={"items": ["reader"]},
        child_specs=child_specs,
        child_dispatch_specs=dispatch_specs,
    )

    assert replay["idempotent_replay"] is True
    dispatch = store.get_entity("hub.edge_dispatch", first["children"][0]["operation_id"])
    assert dispatch is not None
    assert dispatch["record"]["payload"] == child_specs[0]["payload"]
    assert "brief" not in str(first["manifest"])

    corrupted = deepcopy(dispatch["record"])
    corrupted["payload"]["arguments"]["brief"] = "Tampered private brief"
    store.put_entity(
        "hub.edge_dispatch",
        dispatch["entity_id"],
        corrupted,
        expected_revision=dispatch["revision"],
    )
    with pytest.raises(HubStoreV2Conflict, match="batch_child_dispatch_payload_conflict"):
        broker.create_batch_operation(
            logical_target="group-a",
            idempotency_key="dispatch-replay",
            payload={"items": ["reader"]},
            child_specs=child_specs,
            child_dispatch_specs=dispatch_specs,
        )


@pytest.mark.asyncio
async def test_legacy_terminal_parent_cannot_hide_incomplete_manifest(tmp_path):
    store = HubStoreV2(tmp_path / "hub.sqlite3")
    broker = OperationBroker(store)
    parent = broker.create_operation(
        tool="patchbay_worker_start_batch",
        logical_target="group-legacy",
        idempotency_key="legacy-terminal-partial",
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
    parent = broker.prepare_operation(
        parent["operation_id"], expected_revision=parent["revision"]
    )
    parent = broker.make_dispatchable(
        parent["operation_id"], expected_revision=parent["revision"]
    )
    parent = broker.transition_operation(
        parent["operation_id"], expected_revision=parent["revision"], state="running"
    )
    broker.transition_operation(
        parent["operation_id"],
        expected_revision=parent["revision"],
        state="succeeded",
        result={"status": "ok", "result": {"items": [{"item_id": "reader"}]}},
    )

    status = await broker.operation_status(parent["operation_id"], include_result=True)

    assert status["status"] == "blocked"
    assert status["result"]["dispatch"]["state"] == "recovery_required"
    assert status["result"]["outcome"]["terminal"] is True
    assert status["warnings"][0]["details"]["reason"] == "incomplete_atomic_child_set"


def test_legacy_partial_batch_stays_blocked_and_atomic_retry_cannot_fill_it(tmp_path):
    database_path = tmp_path / "hub.sqlite3"
    parent_payload = {"items": ["reader", "writer"]}
    store = HubStoreV2(database_path)
    broker = OperationBroker(store)
    parent = broker.create_operation(
        tool="patchbay_worker_start_batch",
        logical_target="group-a",
        idempotency_key="crash-safe-batch",
        payload=parent_payload,
    )
    broker.declare_child_manifest(
        parent["operation_id"], expected_item_ids=["reader", "writer"]
    )
    parent = broker.prepare_operation(
        parent["operation_id"], expected_revision=parent["revision"]
    )
    parent = broker.make_dispatchable(
        parent["operation_id"], expected_revision=parent["revision"]
    )
    reader = broker.create_child_operation(
        parent["operation_id"],
        item_id="reader",
        tool="patchbay_worker_start",
        logical_target="group-a/reader",
        payload={"name": "Reader"},
    )
    reader = broker.prepare_operation(
        reader["operation_id"], expected_revision=reader["revision"]
    )
    reader = broker.make_dispatchable(
        reader["operation_id"], expected_revision=reader["revision"]
    )
    reader_attempt = _offer_claim_execute(broker, reader)
    _finish(
        broker,
        store,
        reader["operation_id"],
        reader_attempt,
        {"accepted": True, "worker_id": "worker-reader"},
    )

    incomplete = broker.aggregate_parent(parent["operation_id"])
    assert incomplete["state"] == "dispatchable"
    assert incomplete["children_terminal"] is False
    status = asyncio.run(broker.operation_status(parent["operation_id"]))
    assert status["status"] == "blocked"
    assert status["result"]["dispatch"]["state"] == "recovery_required"
    assert status["result"]["safe_next_action"] == "inspect_and_replace_batch"
    assert status["next_actions"][0]["tool"] == "patchbay_operation_status"
    store.close()

    restarted_store = HubStoreV2(database_path)
    restarted_broker = OperationBroker(restarted_store)
    parent_replay = restarted_broker.create_operation(
        tool="patchbay_worker_start_batch",
        logical_target="group-a",
        idempotency_key="crash-safe-batch",
        payload=parent_payload,
    )
    assert parent_replay["idempotent_replay"] is True
    with pytest.raises(
        HubStoreV2StateError,
        match="legacy_batch_incomplete_child_set_recovery_required",
    ):
        restarted_broker.create_batch_operation(
            logical_target="group-a",
            idempotency_key="crash-safe-batch",
            payload=parent_payload,
            child_specs=[
                {
                    "item_id": "reader",
                    "tool": "patchbay_worker_start",
                    "logical_target": "group-a/reader",
                    "payload": {"name": "Reader"},
                },
                {
                    "item_id": "writer",
                    "tool": "patchbay_worker_start",
                    "logical_target": "group-a/writer",
                    "payload": {"name": "Writer"},
                },
            ],
        )
    assert (
        len(restarted_broker.list_child_operations(parent_replay["operation_id"])) == 1
    )
    assert (
        restarted_store.connection.execute(
            """
            SELECT COUNT(*) FROM events
            WHERE operation_id = ?
              AND event_type = 'operation.batch_child_manifest_declared'
            """,
            (parent_replay["operation_id"],),
        ).fetchone()[0]
        == 1
    )


@pytest.mark.asyncio
async def test_legacy_v2_payload_manifest_partial_batch_stays_blocked(tmp_path):
    store = HubStoreV2(tmp_path / "legacy-v2-partial.sqlite3")
    broker = OperationBroker(store)
    parent_payload = {"items": ["reader", "writer"]}
    parent = broker.create_operation(
        tool="patchbay_worker_start_batch",
        logical_target="group-legacy-v2",
        idempotency_key="legacy-v2-partial",
        payload=parent_payload,
    )
    reader_payload = {"brief": "legacy private brief", "repo_path": "/legacy/private"}
    broker.create_child_operation(
        parent["operation_id"],
        item_id="reader",
        tool="patchbay_worker_start",
        logical_target="group-legacy-v2/reader",
        payload=reader_payload,
    )
    legacy_child_specs = [
        {
            "item_id": item_id,
            "tool": "patchbay_worker_start",
            "logical_target": f"group-legacy-v2/{item_id}",
            "payload": payload,
            "semantic_payload_hash": semantic_payload_hash(payload),
        }
        for item_id, payload in (
            ("reader", reader_payload),
            ("writer", {"brief": "missing legacy writer"}),
        )
    ]
    store.put_entity(
        "hub.operation_batch_child_manifest",
        parent["operation_id"],
        {
            "version": 2,
            "operation_id": parent["operation_id"],
            "expected_item_ids": ["reader", "writer"],
            "expected_child_count": 2,
            "child_specs": legacy_child_specs,
            "manifest_hash": semantic_payload_hash({"child_specs": legacy_child_specs}),
        },
        expected_revision=0,
    )

    status = await broker.operation_status(parent["operation_id"])

    assert status["status"] == "blocked"
    assert status["warnings"][0]["details"]["missing_item_ids"] == ["writer"]
    with pytest.raises(
        HubStoreV2StateError,
        match="legacy_batch_incomplete_child_set_recovery_required",
    ):
        broker.create_batch_operation(
            logical_target="group-legacy-v2",
            idempotency_key="legacy-v2-partial",
            payload=parent_payload,
            child_specs=[
                {
                    "item_id": spec["item_id"],
                    "tool": spec["tool"],
                    "logical_target": spec["logical_target"],
                    "payload": spec["payload"],
                }
                for spec in legacy_child_specs
            ],
        )
    assert len(broker.list_child_operations(parent["operation_id"])) == 1


@pytest.mark.asyncio
async def test_batch_parent_status_tracks_children_without_edge_claim_semantics(
    tmp_path,
):
    store = HubStoreV2(tmp_path / "hub.sqlite3")
    broker = OperationBroker(store)
    child_specs = [
        {
            "item_id": item_id,
            "tool": "patchbay_worker_start",
            "logical_target": f"group-a/{item_id}",
            "payload": {
                "action": "codex_worker_start",
                "arguments": {"name": item_id.title()},
            },
        }
        for item_id in ("reader", "writer")
    ]
    batch = broker.create_batch_operation(
        logical_target="group-a",
        idempotency_key="aggregate-status",
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
    parent = batch["parent"]
    children = batch["children"]
    dispatchable_children = []
    for child in children:
        child = broker.prepare_operation(
            child["operation_id"], expected_revision=child["revision"]
        )
        dispatchable_children.append(
            broker.make_dispatchable(
                child["operation_id"], expected_revision=child["revision"]
            )
        )

    active = await broker.operation_status(parent["operation_id"])

    assert active["status"] == "pending"
    assert active["operation"]["state"] == "running"
    assert active["result"]["dispatch"]["state"] == "aggregate_running"
    assert active["result"]["safe_next_action"] == "wait_for_child_operations"
    assert active["next_actions"] == [
        {
            "tool": "patchbay_operation_status",
            "arguments": {
                "operation_id": parent["operation_id"],
                "wait_seconds": 20,
                "since_revision": active["result"]["dispatch"]["event_revision"],
            },
            "reason": "wait_for_child_operations",
        }
    ]
    assert active["result"]["attempt"] == {}
    assert all(child["status"] == "pending" for child in active["result"]["children"])

    attempts = [_offer_claim_execute(broker, child) for child in dispatchable_children]
    _finish(
        broker,
        store,
        dispatchable_children[0]["operation_id"],
        attempts[0],
        {"accepted": True, "worker_id": "worker-reader"},
    )
    partly_complete = await broker.operation_status(parent["operation_id"])
    assert partly_complete["result"]["dispatch"]["state"] == "aggregate_running"
    assert partly_complete["result"]["safe_next_action"] == "wait_for_child_operations"

    _finish(
        broker,
        store,
        dispatchable_children[1]["operation_id"],
        attempts[1],
        {"accepted": False, "reason": "capacity_blocked"},
    )
    complete = await broker.operation_status(
        parent["operation_id"], include_result=True
    )

    assert complete["status"] == "partial"
    assert complete["operation"]["state"] == "succeeded"
    assert complete["result"]["dispatch"]["state"] == "complete"
    assert complete["result"]["safe_next_action"] == "use_domain_result"
    for snapshot in (active, partly_complete, complete):
        assert snapshot["result"]["dispatch"]["state"] != "offered"
        assert snapshot["result"]["safe_next_action"] != "wait_for_edge_claim"
    assert [
        item["status"] for item in complete["result"]["domain_result"]["items"]
    ] == [
        "ok",
        "blocked",
    ]
    assert (
        store.connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE operation_id = ?",
            (parent["operation_id"],),
        ).fetchone()[0]
        == 0
    )


def test_offer_and_claim_require_generation_contract_and_immutable_fence(tmp_path):
    store = HubStoreV2(tmp_path / "hub.sqlite3")
    broker = OperationBroker(store)
    operation = _create_dispatchable(broker)
    attempt = broker.offer_attempt(
        operation["operation_id"],
        machine_id="edge-a",
        edge_generation=9,
        required_contract_hash=CONTRACT_HASH,
    )
    replay = broker.offer_attempt(
        operation["operation_id"],
        machine_id="edge-a",
        edge_generation=9,
        required_contract_hash=CONTRACT_HASH,
    )

    assert replay["attempt_id"] == attempt["attempt_id"]
    assert replay["fencing_token"] == attempt["fencing_token"] == 1
    with pytest.raises(HubStoreV2Conflict, match="edge_generation_mismatch"):
        broker.claim_attempt(
            operation["operation_id"],
            attempt["attempt_id"],
            machine_id="edge-a",
            edge_generation=10,
            contract_hash=CONTRACT_HASH,
            fencing_token=attempt["fencing_token"],
        )
    with pytest.raises(HubStoreV2Conflict, match="edge_contract_mismatch"):
        broker.claim_attempt(
            operation["operation_id"],
            attempt["attempt_id"],
            machine_id="edge-a",
            edge_generation=9,
            contract_hash="old-contract",
            fencing_token=attempt["fencing_token"],
        )
    claimed = broker.claim_attempt(
        operation["operation_id"],
        attempt["attempt_id"],
        machine_id="edge-a",
        edge_generation=9,
        contract_hash=CONTRACT_HASH,
        fencing_token=attempt["fencing_token"],
    )
    assert claimed["attempt_id"] == attempt["attempt_id"]
    assert claimed["fencing_token"] == attempt["fencing_token"]
    assert claimed["state"] == "claimed"


def test_concurrent_claim_retry_returns_one_immutable_attempt(tmp_path):
    path = tmp_path / "hub.sqlite3"
    first_store = HubStoreV2(path, busy_timeout_ms=10_000)
    first = OperationBroker(first_store)
    operation = _create_dispatchable(first)
    attempt = first.offer_attempt(
        operation["operation_id"],
        machine_id="edge-a",
        edge_generation=2,
        required_contract_hash=CONTRACT_HASH,
    )
    second_store = HubStoreV2(path, busy_timeout_ms=10_000)
    second = OperationBroker(second_store)

    def claim(broker: OperationBroker) -> dict:
        return broker.claim_attempt(
            operation["operation_id"],
            attempt["attempt_id"],
            machine_id="edge-a",
            edge_generation=2,
            contract_hash=CONTRACT_HASH,
            fencing_token=attempt["fencing_token"],
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, (first, second)))

    assert {result["attempt_id"] for result in results} == {attempt["attempt_id"]}
    assert {result["fencing_token"] for result in results} == {1}
    assert sum(not result["idempotent_replay"] for result in results) == 1
    assert (
        first_store.connection.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
        == 1
    )
    assert first_store.get_operation(operation["operation_id"])["state"] == "running"


def test_lease_renew_expire_reconcile_retry_and_reject_late_attempt(tmp_path):
    now = [100.0]
    store = HubStoreV2(tmp_path / "hub.sqlite3")
    broker = OperationBroker(store, clock=lambda: now[0])
    operation = _create_dispatchable(broker)
    attempt = broker.offer_attempt(
        operation["operation_id"],
        machine_id="edge-a",
        edge_generation=3,
        required_contract_hash=CONTRACT_HASH,
    )
    attempt = broker.claim_attempt(
        operation["operation_id"],
        attempt["attempt_id"],
        machine_id="edge-a",
        edge_generation=3,
        contract_hash=CONTRACT_HASH,
        fencing_token=attempt["fencing_token"],
        lease_seconds=10,
    )
    now[0] = 105.0
    attempt = broker.renew_lease(
        operation["operation_id"],
        attempt["attempt_id"],
        expected_revision=attempt["revision"],
        machine_id="edge-a",
        edge_generation=3,
        contract_hash=CONTRACT_HASH,
        fencing_token=attempt["fencing_token"],
        lease_seconds=10,
    )
    assert attempt["lease_expires_at"] == 115.0
    attempt = broker.mark_attempt_executing(
        operation["operation_id"],
        attempt["attempt_id"],
        expected_revision=attempt["revision"],
        machine_id="edge-a",
        edge_generation=3,
        contract_hash=CONTRACT_HASH,
        fencing_token=attempt["fencing_token"],
    )

    now[0] = 116.0
    expired = broker.expire_leases()
    assert [item["attempt_id"] for item in expired] == [attempt["attempt_id"]]
    assert store.get_operation(operation["operation_id"])["state"] == "outcome_unknown"
    attempt = broker.begin_reconciliation(
        operation["operation_id"],
        attempt["attempt_id"],
        expected_revision=expired[0]["revision"],
        machine_id="edge-a",
        edge_generation=3,
        contract_hash=CONTRACT_HASH,
        fencing_token=attempt["fencing_token"],
    )
    attempt = broker.complete_reconciliation(
        operation["operation_id"],
        attempt["attempt_id"],
        disposition="retryable",
        expected_revision=attempt["revision"],
        machine_id="edge-a",
        edge_generation=3,
        contract_hash=CONTRACT_HASH,
        fencing_token=attempt["fencing_token"],
    )
    retry = broker.offer_attempt(
        operation["operation_id"],
        machine_id="edge-a",
        edge_generation=3,
        required_contract_hash=CONTRACT_HASH,
    )
    assert retry["fencing_token"] == 2

    with pytest.raises(HubStoreV2Conflict, match="stale_fencing_token"):
        broker.finish_operation(
            operation["operation_id"],
            attempt["attempt_id"],
            expected_revision=store.get_operation(operation["operation_id"])[
                "revision"
            ],
            machine_id="edge-a",
            edge_generation=3,
            contract_hash=CONTRACT_HASH,
            fencing_token=attempt["fencing_token"],
            result={"accepted": True, "worker_id": "too-late"},
        )
    assert store.get_operation(operation["operation_id"])["state"] == "reconciling"
    assert store.list_events()[-1]["event_type"] == "operation.stale_receipt_rejected"

    retry = broker.claim_attempt(
        operation["operation_id"],
        retry["attempt_id"],
        machine_id="edge-a",
        edge_generation=3,
        contract_hash=CONTRACT_HASH,
        fencing_token=retry["fencing_token"],
    )
    retry = broker.mark_attempt_executing(
        operation["operation_id"],
        retry["attempt_id"],
        expected_revision=retry["revision"],
        machine_id="edge-a",
        edge_generation=3,
        contract_hash=CONTRACT_HASH,
        fencing_token=retry["fencing_token"],
    )
    completed = _finish(
        broker,
        store,
        operation["operation_id"],
        retry,
        {"accepted": True, "worker_id": "worker-after-reconcile"},
    )
    assert completed["state"] == "succeeded"
    assert completed["result"]["result"]["worker_id"] == "worker-after-reconcile"


def test_finish_cas_normalizes_result_and_lost_response_retry_is_idempotent(tmp_path):
    path = tmp_path / "hub.sqlite3"
    first_store = HubStoreV2(path)
    first = OperationBroker(first_store)
    operation = _create_dispatchable(first)
    attempt = _offer_claim_execute(first, operation)
    operation_revision = first_store.get_operation(operation["operation_id"])[
        "revision"
    ]

    completed = first.finish_operation(
        operation["operation_id"],
        attempt["attempt_id"],
        expected_revision=operation_revision,
        expected_attempt_revision=attempt["revision"],
        machine_id=attempt["machine_id"],
        edge_generation=attempt["edge_generation"],
        contract_hash=CONTRACT_HASH,
        fencing_token=attempt["fencing_token"],
        result={"status": "refused", "reason": "active_turn_in_progress"},
    )
    second_store = HubStoreV2(path)
    after_restart = OperationBroker(second_store)
    replay = after_restart.finish_operation(
        operation["operation_id"],
        attempt["attempt_id"],
        expected_revision=operation_revision,
        expected_attempt_revision=attempt["revision"],
        machine_id=attempt["machine_id"],
        edge_generation=attempt["edge_generation"],
        contract_hash=CONTRACT_HASH,
        fencing_token=attempt["fencing_token"],
        result={"reason": "active_turn_in_progress", "status": "refused"},
    )

    assert completed["state"] == "blocked"
    assert completed["result"]["status"] == "blocked"
    assert replay["operation_id"] == completed["operation_id"]
    assert replay["idempotent_replay"] is True
    assert replay["receipt_duplicate"] is True
    assert (
        second_store.list_events()[-1]["event_type"]
        == "operation.terminal_receipt_confirmed"
    )


def test_concurrent_equivalent_finish_race_has_one_result_and_one_replay(tmp_path):
    path = tmp_path / "hub.sqlite3"
    left_store = HubStoreV2(path, busy_timeout_ms=10_000)
    left = OperationBroker(left_store)
    operation = _create_dispatchable(left)
    attempt = _offer_claim_execute(left, operation)
    operation_revision = left_store.get_operation(operation["operation_id"])["revision"]
    right_store = HubStoreV2(path, busy_timeout_ms=10_000)
    right = OperationBroker(right_store)

    def finish(broker: OperationBroker) -> dict:
        return broker.finish_operation(
            operation["operation_id"],
            attempt["attempt_id"],
            expected_revision=operation_revision,
            expected_attempt_revision=attempt["revision"],
            machine_id=attempt["machine_id"],
            edge_generation=attempt["edge_generation"],
            contract_hash=CONTRACT_HASH,
            fencing_token=attempt["fencing_token"],
            result={"accepted": True, "worker_id": "worker-a"},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(finish, (left, right)))

    assert {result["state"] for result in results} == {"succeeded"}
    assert sum(result["idempotent_replay"] for result in results) == 1
    assert (
        left_store.get_operation(operation["operation_id"])["result"]["result"][
            "worker_id"
        ]
        == "worker-a"
    )


def test_conflicting_terminal_receipt_is_audited_rejected_and_never_overwrites(
    tmp_path,
):
    store = HubStoreV2(tmp_path / "hub.sqlite3")
    broker = OperationBroker(store)
    operation = _create_dispatchable(broker)
    attempt = _offer_claim_execute(broker, operation)
    completed = _finish(
        broker,
        store,
        operation["operation_id"],
        attempt,
        {"accepted": True, "worker_id": "worker-original"},
    )

    with pytest.raises(HubStoreV2Conflict, match="conflicting_terminal_receipt"):
        broker.finish_operation(
            operation["operation_id"],
            attempt["attempt_id"],
            expected_revision=completed["revision"],
            machine_id=attempt["machine_id"],
            edge_generation=attempt["edge_generation"],
            contract_hash=CONTRACT_HASH,
            fencing_token=attempt["fencing_token"],
            result={"accepted": True, "worker_id": "worker-conflicting"},
        )

    stored = store.get_operation(operation["operation_id"])
    assert stored["result"]["result"]["worker_id"] == "worker-original"
    assert (
        store.list_events()[-1]["event_type"] == "operation.terminal_receipt_conflict"
    )


def test_restart_recovers_offer_contract_and_continues_same_attempt(tmp_path):
    path = tmp_path / "hub.sqlite3"
    first_store = HubStoreV2(path)
    first = OperationBroker(first_store)
    operation = _create_dispatchable(first)
    offered = first.offer_attempt(
        operation["operation_id"],
        machine_id="edge-a",
        edge_generation=7,
        required_contract_hash=CONTRACT_HASH,
    )
    principal_ref = first_store.principal_ref
    first_store.close()

    reopened_store = HubStoreV2(path)
    reopened = OperationBroker(reopened_store)
    claimed = reopened.claim_attempt(
        operation["operation_id"],
        offered["attempt_id"],
        machine_id="edge-a",
        edge_generation=7,
        contract_hash=CONTRACT_HASH,
        fencing_token=offered["fencing_token"],
    )

    assert reopened_store.principal_ref == principal_ref
    assert claimed["attempt_id"] == offered["attempt_id"]
    assert claimed["required_contract_hash"] == CONTRACT_HASH
    assert claimed["state"] == "claimed"


@pytest.mark.asyncio
async def test_event_revision_wait_is_bounded_and_does_not_hold_transaction(tmp_path):
    path = tmp_path / "hub.sqlite3"
    waiting_store = HubStoreV2(path, busy_timeout_ms=1_000)
    waiting = OperationBroker(waiting_store, poll_interval=0.005, max_wait_seconds=1)
    operation = _create_dispatchable(waiting)
    initial_revision = waiting_store.list_events()[-1]["event_revision"]
    writer_store = HubStoreV2(path, busy_timeout_ms=1_000)
    writer = OperationBroker(writer_store)

    task = asyncio.create_task(
        waiting.wait_for_event_revision(
            operation["operation_id"],
            after_revision=initial_revision,
            timeout_seconds=5,
        )
    )
    await asyncio.sleep(0.02)
    assert waiting_store.connection.in_transaction is False
    writer.offer_attempt(
        operation["operation_id"],
        machine_id="edge-a",
        edge_generation=1,
        required_contract_hash=CONTRACT_HASH,
    )
    changed_revision = await asyncio.wait_for(task, timeout=1)

    assert changed_revision > initial_revision
    assert waiting_store.connection.in_transaction is False
    assert (
        await waiting.wait_for_event_revision(
            operation["operation_id"],
            after_revision=changed_revision,
            timeout_seconds=0.01,
        )
        is None
    )


@pytest.mark.asyncio
async def test_operation_status_enforces_principal_visibility_and_result_opt_in(
    tmp_path,
):
    store = HubStoreV2(tmp_path / "hub.sqlite3")
    broker = OperationBroker(store)
    operation = _create_dispatchable(broker)
    attempt = _offer_claim_execute(broker, operation)
    completed = _finish(
        broker,
        store,
        operation["operation_id"],
        attempt,
        {"accepted": True, "worker_id": "worker-a"},
    )

    hidden = await broker.operation_status(
        operation["operation_id"],
        principal_ref="another-principal",
        include_result=True,
    )
    compact = await broker.operation_status(operation["operation_id"])
    detailed = await broker.operation_status(
        operation["operation_id"], include_result=True
    )

    assert hidden["status"] == "not_found"
    assert hidden["operation"] == {}
    assert compact["status"] == "ok"
    assert compact["result"]["domain_result"] == {}
    assert detailed["result"]["domain_result"]["worker_id"] == "worker-a"
    assert detailed["operation"]["revision"] == completed["revision"]
    assert set(detailed) == {
        "status",
        "result",
        "operation",
        "warnings",
        "next_actions",
    }


def test_payload_ack_expiry_and_cancellation_are_cas_guarded(tmp_path):
    now = [50.0]
    store = HubStoreV2(tmp_path / "hub.sqlite3")
    broker = OperationBroker(store, clock=lambda: now[0])
    operation = broker.create_operation(
        tool="patchbay_worker_inbox",
        logical_target="group-a",
        idempotency_key="payload-key",
        payload={"artifact": "one"},
    )
    payload = broker.register_payload(
        operation["operation_id"],
        payload_kind="artifact",
        checksum_sha256="a" * 64,
        size_bytes=12,
        storage_ref="private://payload/one",
        expires_at=60.0,
    )
    replay = broker.register_payload(
        operation["operation_id"],
        payload_kind="artifact",
        checksum_sha256="a" * 64,
        size_bytes=12,
        storage_ref="private://payload/one",
        expires_at=60.0,
    )
    acknowledged = broker.acknowledge_payload(
        payload["payload_id"], expected_revision=payload["revision"]
    )

    assert replay["payload_id"] == payload["payload_id"]
    assert acknowledged["status"] == "acknowledged"
    assert (
        broker.acknowledge_payload(
            payload["payload_id"], expected_revision=payload["revision"]
        )["idempotent_replay"]
        is True
    )

    second = broker.register_payload(
        operation["operation_id"],
        payload_kind="brief",
        checksum_sha256="b" * 64,
        size_bytes=5,
        storage_ref="private://payload/two",
        expires_at=55.0,
    )
    now[0] = 56.0
    assert [item["payload_id"] for item in broker.expire_payloads()] == [
        second["payload_id"]
    ]
    current = store.get_operation(operation["operation_id"])
    cancelled = broker.cancel_operation(
        operation["operation_id"],
        expected_revision=current["revision"],
        reason="operator_cancelled",
    )
    assert cancelled["state"] == "cancelled"
    assert cancelled["result"]["status"] == "blocked"


def test_invalid_operation_and_attempt_transitions_are_rejected(tmp_path):
    store = HubStoreV2(tmp_path / "hub.sqlite3")
    broker = OperationBroker(store)
    operation = broker.create_operation(
        tool="patchbay_worker_start",
        logical_target="group-a",
        idempotency_key="transition-key",
        payload={"name": "Reader"},
    )
    with pytest.raises(HubStoreV2StateError, match="Invalid operation transition"):
        broker.transition_operation(
            operation["operation_id"],
            expected_revision=operation["revision"],
            state="running",
        )

    operation = broker.prepare_operation(
        operation["operation_id"], expected_revision=operation["revision"]
    )
    operation = broker.make_dispatchable(
        operation["operation_id"], expected_revision=operation["revision"]
    )
    attempt = broker.offer_attempt(
        operation["operation_id"],
        machine_id="edge-a",
        edge_generation=1,
        required_contract_hash=CONTRACT_HASH,
    )
    with pytest.raises(HubStoreV2StateError, match="Invalid attempt transition"):
        broker.transition_attempt(
            operation["operation_id"],
            attempt["attempt_id"],
            expected_revision=attempt["revision"],
            machine_id="edge-a",
            edge_generation=1,
            contract_hash=CONTRACT_HASH,
            fencing_token=attempt["fencing_token"],
            state="effect_recorded",
        )


@pytest.mark.asyncio
async def test_persisted_internal_next_action_is_replaced_with_public_operation_status(tmp_path):
    store = HubStoreV2(tmp_path / "persisted-next-action.sqlite3")
    broker = OperationBroker(store)
    operation = broker.create_operation(
        tool="patchbay_worker_start",
        logical_target="group-persisted-action",
        idempotency_key="persisted-next-action",
        payload={"name": "Builder"},
    )
    operation = broker.prepare_operation(
        operation["operation_id"], expected_revision=operation["revision"]
    )
    operation = broker.make_dispatchable(
        operation["operation_id"], expected_revision=operation["revision"]
    )
    operation = broker.transition_operation(
        operation["operation_id"], expected_revision=operation["revision"], state="running"
    )
    assert operation is not None
    completed = broker.transition_operation(
        operation["operation_id"],
        expected_revision=operation["revision"],
        state="succeeded",
        result={
            "status": "ok",
            "result": {"worker": {"name": "Builder"}},
            "operation": {},
            "warnings": [],
            "next_actions": [
                {
                    "tool": "complete_reconciliation",
                    "arguments": {"attempt_id": "untrusted"},
                    "reason": "Invoke the internal transition.",
                }
            ],
        },
    )
    assert completed is not None

    result = await broker.operation_status(operation["operation_id"], include_result=True)

    assert result["next_actions"] == [
        {
            "tool": "patchbay_operation_status",
            "arguments": {"operation_id": operation["operation_id"]},
            "reason": "Inspect this operation through Hub's public recovery tool.",
        }
    ]
    assert "complete_reconciliation" not in str(result)


@pytest.mark.asyncio
async def test_persisted_known_tool_with_invalid_arguments_uses_safe_public_fallback(tmp_path):
    store = HubStoreV2(tmp_path / "persisted-invalid-known-action.sqlite3")
    broker = OperationBroker(store)
    operation = broker.create_operation(
        tool="patchbay_worker_start",
        logical_target="group-invalid-known-action",
        idempotency_key="persisted-invalid-known-action",
        payload={"name": "Builder"},
    )
    operation = broker.prepare_operation(
        operation["operation_id"], expected_revision=operation["revision"]
    )
    operation = broker.make_dispatchable(
        operation["operation_id"], expected_revision=operation["revision"]
    )
    operation = broker.transition_operation(
        operation["operation_id"],
        expected_revision=operation["revision"],
        state="running",
    )
    assert operation is not None
    completed = broker.transition_operation(
        operation["operation_id"],
        expected_revision=operation["revision"],
        state="succeeded",
        result={
            "status": "ok",
            "result": {"worker": {"name": "Builder"}},
            "operation": {},
            "warnings": [],
            "next_actions": [{"tool": "patchbay_worker_wait"}],
        },
    )
    assert completed is not None

    result = await broker.operation_status(operation["operation_id"], include_result=True)

    assert result["next_actions"] == [
        {
            "tool": "patchbay_operation_status",
            "arguments": {"operation_id": operation["operation_id"]},
            "reason": "Inspect this operation through Hub's public recovery tool.",
        }
    ]
    assert "patchbay_worker_wait" not in str(result["next_actions"])


@pytest.mark.asyncio
async def test_stale_integration_preview_returns_complete_public_review_action(tmp_path):
    store = HubStoreV2(tmp_path / "stale-integration-preview.sqlite3")
    broker = OperationBroker(store)
    operation = broker.create_operation(
        tool="patchbay_worker_integrate",
        logical_target="fworker_writer_1",
        idempotency_key="stale-preview-integrate",
        payload={"preview_token": "old-token"},
    )
    broker.associate_operation(
        operation["operation_id"], work_group_id="group_integration"
    )
    operation = broker.prepare_operation(
        operation["operation_id"], expected_revision=operation["revision"]
    )
    operation = broker.make_dispatchable(
        operation["operation_id"], expected_revision=operation["revision"]
    )
    attempt = _offer_claim_execute(broker, operation)
    _finish(
        broker,
        store,
        operation["operation_id"],
        attempt,
        {
            "applied": False,
            "can_apply": False,
            "reason": "stale_preview_token",
            "fresh_preview": {"preview_token": "pit2.replacement", "can_apply": True},
            "recommended_next_action": "review_fresh_integration_preview",
            "next_tool": "codex_worker_integrate",
            "next_arguments": {"preview_token": "pit2.replacement"},
        },
    )

    result = await broker.operation_status(
        operation["operation_id"], include_result=True
    )

    assert result["status"] == "blocked"
    assert result["result"]["domain_result"]["reason"] == "stale_preview_token"
    assert "next_tool" not in result["result"]["domain_result"]
    assert "next_arguments" not in result["result"]["domain_result"]
    assert result["next_actions"] == [
        {
            "tool": "patchbay_worker_inspect",
            "arguments": {
                "work_group_id": "group_integration",
                "fleet_worker_ref": "fworker_writer_1",
                "view": "integration_preview",
            },
            "reason": (
                "The signed preview became stale. Review the authoritative replacement "
                "preview, then submit a new integration mutation with its token and a fresh "
                "idempotency key."
            ),
        }
    ]
