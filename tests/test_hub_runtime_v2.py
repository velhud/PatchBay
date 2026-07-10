from __future__ import annotations

import asyncio

from patchbay.hub.runtime_v2 import (
    FLEET_WORKER_ENTITY,
    MACHINE_GENERATION_ENTITY,
    WORK_GROUP_ENTITY,
    WORKER_PROJECTION_ENTITY,
    HubRuntimeV2,
)
from patchbay.hub.protocol_v2 import validate_hub_v2_tool_output
from patchbay.hub.store_v2 import HubStoreV2
from patchbay.hub.tool_surface import HUB_V2_CONTRACT_HASH
from patchbay.protocol.context import RequestContext


def context(name: str) -> RequestContext:
    return RequestContext(
        client_ref=f"client_{name}",
        chatgpt_session_ref=f"conversation_{name}",
        work_run_ref=f"run_{name}",
    )


def make_runtime(tmp_path, *, now: float = 1_000.0, routing_enabled: bool = True):
    path = tmp_path / "hub-v2.sqlite3"
    store = HubStoreV2(path)
    runtime = HubRuntimeV2(
        {
            "hub": {
                "heartbeat_stale_seconds": 90,
                "routing": {
                    "enabled": routing_enabled,
                    "min_disk_free_bytes": 0,
                },
            }
        },
        store,
        clock=lambda: now,
    )
    return runtime, store, path


def enroll_online(
    runtime: HubRuntimeV2,
    *,
    machine_id: str,
    workspace_alias: str = "PatchBay",
    workspace_path: str = "/srv/PatchBay",
    git: bool = True,
    active_workers: int = 0,
    free_slots: int = 4,
    tags: list[str] | None = None,
):
    code = runtime.create_enrollment_code(name=machine_id, tags=tags or ["codex"])["code"]
    enrolled = runtime.enroll_machine(
        code=code,
        machine_id=machine_id,
        display_name=machine_id,
        tags=tags or ["codex"],
    )
    heartbeat = runtime.heartbeat(
        machine_id=machine_id,
        token=enrolled["node_token"],
        edge_generation=enrolled["edge_generation"],
        projection_revision=1,
        capabilities={
            "contract_hash": HUB_V2_CONTRACT_HASH,
            "max_concurrent_jobs": active_workers + free_slots,
            "queue_enabled": False,
        },
        workspaces=[
            {
                "alias": workspace_alias,
                "path": workspace_path,
                "exists": True,
                "git": git,
                "repository_identity": "https://example.invalid/patchbay.git"
                if workspace_alias == "PatchBay"
                else "",
            }
        ],
        worker_projection={
            "snapshot_kind": "full",
            "complete_worker_set": True,
            "workers": [],
            "tombstones": [],
        },
        resource_status={
            "active_workers": active_workers,
            "max_concurrent_jobs": active_workers + free_slots,
            "free_worker_slots": free_slots,
            "queue_enabled": False,
            "memory_used_percent": 20,
            "cpu_percent": 10,
            "disk_free_bytes": 10_000_000_000,
        },
    )
    assert heartbeat["projection_accepted"] is True
    return enrolled


def create_group(
    runtime: HubRuntimeV2,
    *,
    caller: RequestContext,
    machine_id: str = "",
    key: str = "group-create-1",
):
    result = runtime.create_work_group(
        title="Implement Hub V2",
        goal="Coordinate the bounded implementation.",
        workspace_ref=runtime.workspace_list()["result"]["workspaces"][0]["workspace_ref"],
        machine_id=machine_id,
        lanes=[{"lane": "implementation", "title": "Implementation", "role": "Build"}],
        idempotency_key=key,
        context=caller,
    )
    assert result["status"] == "ok"
    return result


def heartbeat_workers(runtime: HubRuntimeV2, enrolled: dict, revision: int, workers: list[dict]):
    return runtime.heartbeat(
        machine_id=enrolled["machine"]["machine_id"],
        token=enrolled["node_token"],
        edge_generation=enrolled["edge_generation"],
        projection_revision=revision,
        capabilities={
            "contract_hash": HUB_V2_CONTRACT_HASH,
            "max_concurrent_jobs": 4,
            "queue_enabled": False,
        },
        worker_projection={
            "snapshot_kind": "full",
            "complete_worker_set": True,
            "workers": workers,
            "tombstones": [],
        },
        resource_status={
            "active_workers": sum(worker.get("turn_state") == "working" for worker in workers),
            "max_concurrent_jobs": 4,
            "free_worker_slots": 3,
            "queue_enabled": False,
            "disk_free_bytes": 10_000_000_000,
        },
    )


def test_machine_generation_heartbeat_and_workspace_projection_survive_restart(tmp_path):
    runtime, store, path = make_runtime(tmp_path)
    first = enroll_online(runtime, machine_id="machine_alpha")
    workspace_ref = runtime.workspace_list()["result"]["workspaces"][0]["workspace_ref"]
    principal_ref = store.principal_ref
    store.close()

    reopened_store = HubStoreV2(path)
    restarted = HubRuntimeV2(
        {"hub": {"heartbeat_stale_seconds": 90, "routing": {"min_disk_free_bytes": 0}}},
        reopened_store,
        clock=lambda: 1_001.0,
    )
    fleet = restarted.fleet_status()["result"]
    workspaces = restarted.workspace_list()["result"]["workspaces"]

    assert reopened_store.principal_ref == principal_ref
    assert fleet["machines"][0]["edge_generation"] == first["edge_generation"]
    assert fleet["machines"][0]["status"] == "online"
    assert workspaces[0]["workspace_ref"] == workspace_ref
    assert workspaces[0]["projections"][0]["local_path"] == "/srv/PatchBay"


def test_reenrollment_creates_new_generation_and_preserves_old_generation_record(tmp_path):
    runtime, store, _ = make_runtime(tmp_path)
    first = enroll_online(runtime, machine_id="machine_alpha")
    code = runtime.create_enrollment_code(name="replacement")["code"]
    second = runtime.enroll_machine(code=code, machine_id="machine_alpha", display_name="replacement")

    assert second["edge_generation"] != first["edge_generation"]
    old = store.get_entity(MACHINE_GENERATION_ENTITY, first["edge_generation"])["record"]
    assert old["superseded_by"] == second["edge_generation"]
    assert old["superseded_at"] == 1_000.0


def test_duplicate_projection_revision_is_ignored_without_losing_heartbeat_freshness(tmp_path):
    runtime, _, _ = make_runtime(tmp_path)
    enrolled = enroll_online(runtime, machine_id="machine_alpha")

    duplicate = runtime.heartbeat(
        machine_id="machine_alpha",
        token=enrolled["node_token"],
        edge_generation=enrolled["edge_generation"],
        projection_revision=1,
        capabilities={"contract_hash": "wrong"},
        workspaces=[],
    )

    assert duplicate["projection_accepted"] is False
    assert duplicate["current_projection_revision"] == 1
    assert runtime.fleet_status()["result"]["machines"][0]["compatibility"] == "compatible"
    assert runtime.workspace_list()["result"]["count"] == 1


def test_workspace_matching_prefers_specific_alias_over_generic_root(tmp_path):
    runtime, _, _ = make_runtime(tmp_path)
    enroll_online(
        runtime,
        machine_id="machine_root",
        workspace_alias="repos",
        workspace_path="/workspace/repos",
        git=False,
        active_workers=0,
    )
    enroll_online(
        runtime,
        machine_id="machine_specific",
        workspace_alias="PatchBay",
        workspace_path="/opt/PatchBay",
        git=True,
        active_workers=2,
    )

    created = runtime.create_work_group(
        title="Alias routing",
        goal="Prove the specific advertised repository wins.",
        repo_path="PatchBay",
        idempotency_key="alias-route-1",
        context=context("owner"),
    )

    group = created["result"]["work_group"]
    assert group["pinned_machine_id"] == "machine_specific"
    assert group["resolved_repo_path"] == "/opt/PatchBay"
    assert created["result"]["routing"]["mode"] == "availability_only"


def test_availability_routing_pins_lower_pressure_machine_and_preflight_operation(tmp_path):
    runtime, store, _ = make_runtime(tmp_path)
    enroll_online(runtime, machine_id="machine_busy", active_workers=3, free_slots=1)
    enroll_online(runtime, machine_id="machine_free", active_workers=0, free_slots=4)
    created = create_group(runtime, caller=context("owner"))
    group = created["result"]["work_group"]
    preflight_id = created["result"]["readiness"]["operation_id"]
    preflight = store.get_operation(preflight_id)

    assert group["pinned_machine_id"] == "machine_free"
    assert group["pinned_edge_generation"]
    assert preflight["tool"] == "patchbay_edge_preflight"
    assert preflight["logical_target"] == group["work_group_id"]
    assert preflight["state"] == "dispatchable"
    status = asyncio.run(runtime.operation_status(operation_id=preflight_id, context=context("owner")))
    assert status["status"] == "pending"
    assert status["result"]["dispatch"]["state"] == "offered"
    assert status["operation"]["parent_operation_id"] == ""
    validate_hub_v2_tool_output("patchbay_operation_status", status)

    runtime._clock = lambda: 1_200.0
    degraded = runtime.work_group_status(
        work_group_id=group["work_group_id"], context=context("owner")
    )
    assert degraded["result"]["readiness"]["status"] == "machine_unavailable"
    assert degraded["result"]["work_group"]["pinned_machine_id"] == "machine_free"


def test_routing_disabled_blocks_implicit_placement_and_is_reported(tmp_path):
    runtime, _, _ = make_runtime(tmp_path, routing_enabled=False)
    enroll_online(runtime, machine_id="machine_alpha")

    workspace_ref = runtime.workspace_list()["result"]["workspaces"][0]["workspace_ref"]
    implicit = runtime.create_work_group(
        title="Implicit placement",
        goal="Prove disabled routing is honored.",
        workspace_ref=workspace_ref,
        idempotency_key="implicit-routing-disabled",
        context=context("owner"),
    )
    explicit = create_group(
        runtime,
        caller=context("owner"),
        machine_id="machine_alpha",
        key="explicit-routing-disabled",
    )
    fleet = runtime.fleet_status(context=context("owner"))

    assert implicit["status"] == "blocked"
    assert implicit["result"]["reason"] == "routing_disabled"
    assert implicit["result"]["routing_enabled"] is False
    assert explicit["status"] == "ok"
    assert explicit["result"]["routing"]["mode"] == "explicit_machine"
    assert fleet["result"]["routing_enabled"] is False


def test_preflight_result_is_strict_and_does_not_change_group_pin(tmp_path):
    runtime, store, _ = make_runtime(tmp_path)
    enroll_online(runtime, machine_id="machine_alpha")
    created = create_group(runtime, caller=context("owner"))
    group = created["result"]["work_group"]
    preflight_id = created["result"]["readiness"]["operation_id"]

    result = runtime.record_preflight_result(
        work_group_id=group["work_group_id"],
        operation_id=preflight_id,
        result={
            "ok": True,
            "repo_exists": True,
            "repo_resolved": "/different/path",
            "disk_free_bytes": 10_000_000_000,
            "free_worker_slots": 2,
        },
    )
    persisted = store.get_entity(WORK_GROUP_ENTITY, group["work_group_id"])["record"]

    assert result["result"]["readiness"]["status"] == "failed"
    assert "workspace_path_mismatch" in result["result"]["readiness"]["blockers"]
    assert persisted["pinned_machine_id"] == group["pinned_machine_id"]
    assert persisted["pinned_edge_generation"] == group["pinned_edge_generation"]
    assert store.get_operation(preflight_id)["state"] == "blocked"


def test_participant_current_group_mapping_and_takeover_coordination_survive_restart(tmp_path):
    runtime, store, path = make_runtime(tmp_path)
    enroll_online(runtime, machine_id="machine_alpha")
    first_context = context("first")
    second_context = context("second")
    created = create_group(runtime, caller=first_context)
    group_id = created["result"]["work_group"]["work_group_id"]

    visible = runtime.list_work_groups(scope="owned", context=second_context)
    refused = runtime.resume_work_group(
        work_group_id=group_id,
        idempotency_key="resume-second-refused",
        context=second_context,
    )
    resumed = runtime.resume_work_group(
        work_group_id=group_id,
        takeover=True,
        takeover_reason="Continue from the second conversation.",
        idempotency_key="resume-second-ok",
        context=second_context,
    )
    store.close()
    restarted_store = HubStoreV2(path)
    restarted = HubRuntimeV2(
        {"hub": {"heartbeat_stale_seconds": 90, "routing": {"min_disk_free_bytes": 0}}},
        restarted_store,
        clock=lambda: 1_001.0,
    )

    assert [group["work_group_id"] for group in visible["result"]["work_groups"]] == [group_id]
    assert refused["status"] == "blocked"
    assert refused["result"]["reason"] == "active_participant_requires_takeover"
    assert resumed["result"]["work_group"]["active_participant_ref"] == "conversation_second"
    assert restarted.list_work_groups(scope="current", context=second_context)["result"]["work_groups"][0][
        "work_group_id"
    ] == group_id


def test_close_refuses_active_worker_then_closes_from_authoritative_projection(tmp_path):
    runtime, store, _ = make_runtime(tmp_path)
    enrolled = enroll_online(runtime, machine_id="machine_alpha")
    caller = context("owner")
    created = create_group(runtime, caller=caller)
    group_id = created["result"]["work_group"]["work_group_id"]
    active = {
        "edge_worker_id": "worker-1",
        "name": "Implementer",
        "work_group_id": group_id,
        "lane_id": "implementation",
        "worker_state": "available",
        "turn_state": "working",
        "liveness": "active",
        "integration_state": "no_changes",
        "review_disposition": "not_required",
    }
    heartbeat_workers(runtime, enrolled, 2, [active])
    worker_ref = store.list_entities(FLEET_WORKER_ENTITY)[0]["entity_id"]

    refused = runtime.close_work_group(
        work_group_id=group_id,
        outcome="complete",
        summary="Premature close.",
        worker_dispositions={worker_ref: "no_changes"},
        active_work_disposition="refuse",
        idempotency_key="close-refused",
        context=caller,
    )
    completed = {**active, "turn_state": "completed", "liveness": "terminal"}
    heartbeat_workers(runtime, enrolled, 3, [completed])
    closed = runtime.close_work_group(
        work_group_id=group_id,
        outcome="complete",
        summary="Authoritative worker projection is complete.",
        worker_dispositions={worker_ref: "no_changes"},
        active_work_disposition="refuse",
        idempotency_key="close-success",
        context=caller,
    )

    assert refused["status"] == "blocked"
    assert refused["result"]["reason"] == "close_disposition_refused"
    assert closed["status"] == "ok"
    assert closed["result"]["work_group"]["status"] == "closed"
    assert runtime.list_work_groups(scope="current", context=caller)["result"]["work_groups"] == []


def test_close_requires_explicit_discard_consent_for_unintegrated_changes(tmp_path):
    runtime, store, _ = make_runtime(tmp_path)
    enrolled = enroll_online(runtime, machine_id="machine_alpha")
    caller = context("owner")
    created = create_group(runtime, caller=caller)
    group_id = created["result"]["work_group"]["work_group_id"]
    worker = {
        "edge_worker_id": "worker-change",
        "name": "Writer",
        "work_group_id": group_id,
        "lane_id": "implementation",
        "worker_state": "available",
        "turn_state": "completed",
        "liveness": "terminal",
        "integration_state": "not_integrated",
        "review_disposition": "accepted",
    }
    heartbeat_workers(runtime, enrolled, 2, [worker])
    worker_ref = store.list_entities(WORKER_PROJECTION_ENTITY)[0]["entity_id"]

    refused = runtime.close_work_group(
        work_group_id=group_id,
        outcome="abandoned",
        summary="Discard without consent must fail.",
        worker_dispositions=[{"fleet_worker_ref": worker_ref, "disposition": "discarded"}],
        idempotency_key="discard-refused",
        context=caller,
    )
    accepted = runtime.close_work_group(
        work_group_id=group_id,
        outcome="abandoned",
        summary="Explicitly discard the unintegrated changes.",
        worker_dispositions=[
            {
                "fleet_worker_ref": worker_ref,
                "disposition": "discarded",
                "discard_unintegrated_changes": True,
            }
        ],
        idempotency_key="discard-accepted",
        context=caller,
    )

    assert refused["status"] == "blocked"
    assert accepted["status"] == "ok"
    assert accepted["result"]["work_group"]["closure_dispositions"] == {
        worker_ref: "discarded_explicitly"
    }


def test_reassign_creates_successor_and_preserves_predecessor_worker_route(tmp_path):
    runtime, store, _ = make_runtime(tmp_path)
    old_edge = enroll_online(runtime, machine_id="machine_old")
    enroll_online(runtime, machine_id="machine_new")
    caller = context("owner")
    created = create_group(runtime, caller=caller, machine_id="machine_old")
    old_group = created["result"]["work_group"]
    worker = {
        "edge_worker_id": "worker-old",
        "name": "Old Worker",
        "work_group_id": old_group["work_group_id"],
        "lane_id": "implementation",
        "worker_state": "available",
        "turn_state": "completed",
        "liveness": "terminal",
        "integration_state": "no_changes",
        "review_disposition": "not_required",
    }
    heartbeat_workers(runtime, old_edge, 2, [worker])
    old_worker_ref = store.list_entities(FLEET_WORKER_ENTITY)[0]["entity_id"]

    reassigned = runtime.reassign_work_group(
        work_group_id=old_group["work_group_id"],
        machine_id="machine_new",
        reason="The old machine is being replaced.",
        idempotency_key="successor-1",
        context=caller,
    )
    successor = reassigned["result"]["work_group"]
    predecessor = store.get_entity(WORK_GROUP_ENTITY, old_group["work_group_id"])["record"]
    old_status = runtime.work_group_status(work_group_id=old_group["work_group_id"], context=caller)

    assert predecessor["pinned_machine_id"] == "machine_old"
    assert predecessor["pinned_edge_generation"] == old_group["pinned_edge_generation"]
    assert predecessor["status"] == "superseded"
    assert successor["pinned_machine_id"] == "machine_new"
    assert successor["supersedes"] == old_group["work_group_id"]
    assert successor["worker_refs"] == []
    assert old_worker_ref in old_status["result"]["work_group"]["worker_refs"]


def test_reassign_cancels_group_associated_unclaimed_worker_operations(tmp_path):
    runtime, store, _ = make_runtime(tmp_path)
    enroll_online(runtime, machine_id="machine_old")
    enroll_online(runtime, machine_id="machine_new")
    caller = context("owner")
    created = create_group(runtime, caller=caller, machine_id="machine_old")
    group_id = created["result"]["work_group"]["work_group_id"]
    operation = runtime.broker.create_operation(
        tool="patchbay_worker_start",
        logical_target="worker-target",
        idempotency_key="worker-before-reassign",
        payload={"name": "Worker"},
    )
    runtime.broker.associate_operation(operation["operation_id"], work_group_id=group_id)
    operation = runtime.broker.prepare_operation(
        operation["operation_id"], expected_revision=operation["revision"]
    )
    operation = runtime.broker.make_dispatchable(
        operation["operation_id"], expected_revision=operation["revision"]
    )

    reassigned = runtime.reassign_work_group(
        work_group_id=group_id,
        machine_id="machine_new",
        reason="Move remaining work.",
        idempotency_key="reassign-cancels-unclaimed",
        context=caller,
    )

    assert reassigned["status"] == "ok"
    assert store.get_operation(operation["operation_id"])["state"] == "cancelled"
    old_status = runtime.work_group_status(work_group_id=group_id, context=caller)
    operation_ids = {item["operation_id"] for item in old_status["result"]["operations"]}
    assert operation["operation_id"] in operation_ids


def test_close_refuses_group_associated_running_worker_operation(tmp_path):
    runtime, _, _ = make_runtime(tmp_path)
    enroll_online(runtime, machine_id="machine_alpha")
    caller = context("owner")
    created = create_group(runtime, caller=caller, machine_id="machine_alpha")
    group_id = created["result"]["work_group"]["work_group_id"]
    operation = runtime.broker.create_operation(
        tool="patchbay_worker_message",
        logical_target="fleet-worker-ref",
        idempotency_key="running-worker-operation",
        payload={"message": "Continue."},
    )
    runtime.broker.associate_operation(operation["operation_id"], work_group_id=group_id)
    for state in ("payload_ready", "dispatchable", "running"):
        operation = runtime.broker.transition_operation(
            operation["operation_id"],
            expected_revision=operation["revision"],
            state=state,
        )

    closed = runtime.close_work_group(
        work_group_id=group_id,
        outcome="abandoned",
        summary="Do not close claimed work.",
        worker_dispositions={},
        active_work_disposition="leave_running",
        idempotency_key="close-running-operation",
        context=caller,
    )

    assert closed["status"] == "blocked"
    assert {
        blocker["reason"]
        for blocker in closed["result"]["validation"]["blockers"]
    } == {"active_or_uncertain_operations"}


def test_adapter_registration_dispatches_only_unimplemented_tool_families(tmp_path):
    runtime, _, _ = make_runtime(tmp_path)
    calls = []

    async def adapter(name, arguments, *, context=None):
        calls.append((name, dict(arguments), context))
        return {
            "status": "ok",
            "result": {"workers": []},
            "operation": {},
            "warnings": [],
            "next_actions": [],
        }

    missing = asyncio.run(runtime.handle_tool_call("patchbay_worker_list", {}))
    runtime.register_adapter("workers_and_artifacts", adapter)
    routed = asyncio.run(
        runtime.handle_tool_call(
            "patchbay_worker_list",
            {"work_group_id": "group_test"},
            context=context("owner"),
        )
    )

    assert missing["status"] == "blocked"
    assert missing["result"]["reason"] == "tool_family_adapter_not_registered"
    assert routed["status"] == "ok"
    assert calls == [
        (
            "patchbay_worker_list",
            {"work_group_id": "group_test"},
            context("owner"),
        )
    ]
