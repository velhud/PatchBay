from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from patchbay.hub.broker import OperationBroker
from patchbay.hub.store_v2 import HubStoreV2, HubStoreV2Conflict, HubStoreV2StateError


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
    parent = broker.create_operation(
        tool="patchbay_worker_start_batch",
        logical_target="group-a",
        idempotency_key="batch-key",
        payload={"items": ["reader", "writer"]},
    )
    reader = broker.create_child_operation(
        parent["operation_id"],
        item_id="reader",
        tool="patchbay_worker_start",
        logical_target="group-a/reader",
        payload={"name": "Reader"},
    )
    reader_replay = broker.create_child_operation(
        parent["operation_id"],
        item_id="reader",
        tool="patchbay_worker_start",
        logical_target="group-a/reader",
        payload={"name": "Reader"},
    )
    writer = broker.create_child_operation(
        parent["operation_id"],
        item_id="writer",
        tool="patchbay_worker_start",
        logical_target="group-a/writer",
        payload={"name": "Writer"},
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
