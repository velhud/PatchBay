from __future__ import annotations

import asyncio
import json

import pytest

from patchbay.hub.runtime_v2 import (
    FLEET_WORKER_ENTITY,
    MACHINE_GENERATION_ENTITY,
    OPERATION_GROUP_ENTITY,
    WORK_GROUP_ENTITY,
    WORKER_PROJECTION_ENTITY,
    HubRuntimeV2,
)
from patchbay.hub.protocol_v2 import validate_hub_v2_tool_output
from patchbay.hub.store_v2 import HubStoreV2, HubStoreV2Conflict
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
    assert fleet["machines"][0]["worker_summary"]["projection_revision"] == 1
    assert workspaces[0]["workspace_ref"] == workspace_ref
    assert workspaces[0]["projections"][0]["local_path"] == "/srv/PatchBay"


def test_projection_only_update_refreshes_compact_machine_worker_summary(tmp_path):
    runtime, _, _ = make_runtime(tmp_path)
    enrolled = enroll_online(runtime, machine_id="machine_alpha")
    worker = {
        "edge_worker_id": "wrk_projection",
        "name": "Projection worker",
        "turn_state": "working",
        "liveness": "active",
        "integration_state": "no_changes",
    }
    heartbeat_workers(runtime, enrolled, 2, [worker])

    completed = dict(worker, turn_state="completed", liveness="terminal")
    heartbeat_workers(runtime, enrolled, 3, [completed])

    machine = runtime.fleet_status()["result"]["machines"][0]
    assert "worker_status" not in machine
    assert machine["worker_summary"]["projection_revision"] == 3
    assert machine["worker_summary"]["counts"] == {
        "total": 1,
        "active": 0,
        "quiet": 0,
        "stale": 0,
        "lost": 0,
        "failed": 0,
        "completed": 1,
        "unintegrated": 0,
    }


def test_compact_machine_worker_summary_uses_canonical_worker_states():
    summary = HubRuntimeV2._fleet_worker_summary(
        {
            "workers": [
                {
                    "turn_state": "completed",
                    "liveness": "terminal",
                    "integration_state": "not_integrated",
                },
                {
                    "turn_state": "failed",
                    "liveness": "terminal",
                    "integration_state": "uncertain",
                },
                {
                    "turn_state": "working",
                    "liveness": "quiet",
                    "integration_state": "not_applicable",
                },
                "malformed",
            ]
        }
    )

    assert summary["counts"] == {
        "total": 3,
        "active": 1,
        "quiet": 1,
        "stale": 0,
        "lost": 0,
        "failed": 1,
        "completed": 1,
        "unintegrated": 2,
    }


def test_rejected_delta_does_not_replace_authoritative_fleet_projection(tmp_path):
    runtime, _, _ = make_runtime(tmp_path)
    enrolled = enroll_online(runtime, machine_id="machine_alpha")
    completed = [
        {
            "edge_worker_id": f"wrk_{index}",
            "turn_state": "completed",
            "liveness": "terminal",
            "integration_state": "no_changes",
        }
        for index in range(2)
    ]
    heartbeat_workers(runtime, enrolled, 2, completed)

    rejected = runtime.heartbeat(
        machine_id="machine_alpha",
        token=enrolled["node_token"],
        edge_generation=enrolled["edge_generation"],
        projection_revision=4,
        worker_projection={
            "snapshot_kind": "delta",
            "workers": [
                {
                    "edge_worker_id": "wrk_rejected",
                    "turn_state": "failed",
                    "liveness": "terminal",
                    "integration_state": "uncertain",
                }
            ],
            "tombstones": [],
        },
    )

    assert rejected["projection_accepted"] is False
    assert rejected["request_full_snapshot"] is True
    fleet = runtime.fleet_status()["result"]["machines"][0]
    assert fleet["projection_revision"] == 2
    assert fleet["worker_summary"]["projection_revision"] == 2
    assert fleet["worker_summary"]["last_received_projection_revision"] == 4
    assert fleet["worker_summary"]["resync_required"] is True
    assert fleet["worker_projection_status"] == "resync_required"
    assert fleet["worker_summary"]["counts"]["completed"] == 2
    assert fleet["worker_summary"]["counts"]["failed"] == 0

    accepted = runtime.heartbeat(
        machine_id="machine_alpha",
        token=enrolled["node_token"],
        edge_generation=enrolled["edge_generation"],
        projection_revision=4,
        worker_projection={
            "snapshot_kind": "full",
            "complete_worker_set": True,
            "workers": [
                {
                    "edge_worker_id": "wrk_replacement",
                    "turn_state": "failed",
                    "liveness": "terminal",
                    "integration_state": "uncertain",
                }
            ],
            "tombstones": [],
        },
    )
    assert accepted["projection_accepted"] is True
    fleet = runtime.fleet_status()["result"]["machines"][0]
    assert fleet["projection_revision"] == 4
    assert fleet["worker_summary"]["projection_revision"] == 4
    assert fleet["worker_summary"]["resync_required"] is False
    assert fleet["worker_summary"]["counts"]["failed"] == 1
    assert fleet["worker_summary"]["counts"]["total"] == 1
    assert fleet["worker_summary"]["counts"]["completed"] == 0
    assert fleet["worker_summary"]["counts"]["lost"] == 0
    assert fleet["worker_summary"]["tombstone_count"] == 2


def test_worker_projection_conflict_rolls_back_entire_revision_and_can_retry(
    tmp_path,
):
    runtime, store, _ = make_runtime(tmp_path)
    enrolled = enroll_online(runtime, machine_id="machine_alpha")
    original_workers = [
        {
            "edge_worker_id": worker_id,
            "lane_id": "main",
            "turn_state": "completed",
            "liveness": "terminal",
            "integration_state": "no_changes",
        }
        for worker_id in ("wrk_a", "wrk_b")
    ]
    heartbeat_workers(runtime, enrolled, 2, original_workers)

    with pytest.raises(HubStoreV2Conflict, match="immutable_fleet_worker_lane_id"):
        runtime.heartbeat(
            machine_id="machine_alpha",
            token=enrolled["node_token"],
            edge_generation=enrolled["edge_generation"],
            projection_revision=3,
            worker_projection={
                "snapshot_kind": "full",
                "complete_worker_set": True,
                "workers": [
                    {
                        "edge_worker_id": "wrk_c",
                        "lane_id": "main",
                        "turn_state": "completed",
                        "liveness": "terminal",
                        "integration_state": "no_changes",
                    },
                    {
                        **original_workers[1],
                        "lane_id": "conflicting-lane",
                    },
                ],
                "tombstones": [],
            },
        )

    fleet = runtime.fleet_status()["result"]["machines"][0]
    assert fleet["projection_revision"] == 2
    assert fleet["worker_summary"]["projection_revision"] == 2
    assert fleet["worker_summary"]["counts"]["total"] == 2
    assert all(
        entity["record"].get("edge_worker_id") != "wrk_c"
        for entity in store.list_entities(FLEET_WORKER_ENTITY)
    )

    corrected = runtime.heartbeat(
        machine_id="machine_alpha",
        token=enrolled["node_token"],
        edge_generation=enrolled["edge_generation"],
        projection_revision=3,
        worker_projection={
            "snapshot_kind": "full",
            "complete_worker_set": True,
            "workers": [
                {
                    "edge_worker_id": "wrk_c",
                    "lane_id": "main",
                    "turn_state": "completed",
                    "liveness": "terminal",
                    "integration_state": "no_changes",
                },
                original_workers[1],
            ],
            "tombstones": [],
        },
    )
    assert corrected["projection_accepted"] is True
    fleet = runtime.fleet_status()["result"]["machines"][0]
    assert fleet["projection_revision"] == 3
    assert fleet["worker_summary"]["projection_revision"] == 3
    assert fleet["worker_summary"]["counts"]["total"] == 2
    assert fleet["worker_summary"]["tombstone_count"] == 1


def test_malformed_projection_is_rejected_before_machine_state_changes(tmp_path):
    runtime, _, _ = make_runtime(tmp_path)
    enrolled = enroll_online(runtime, machine_id="machine_alpha")

    with pytest.raises(ValueError, match="tombstones must be a list"):
        runtime.heartbeat(
            machine_id="machine_alpha",
            token=enrolled["node_token"],
            edge_generation=enrolled["edge_generation"],
            projection_revision=2,
            worker_projection={
                "snapshot_kind": "full",
                "workers": [],
                "tombstones": 7,
            },
        )

    fleet_envelope = runtime.fleet_status()
    fleet = fleet_envelope["result"]
    assert fleet["machines"][0]["projection_revision"] == 1
    assert fleet["machines"][0]["worker_summary"]["counts"]["total"] == 0
    validate_hub_v2_tool_output("patchbay_fleet_status", fleet_envelope)


def test_malformed_workspace_snapshot_is_rejected_before_revision_advances(
    tmp_path,
):
    runtime, _, _ = make_runtime(tmp_path)
    enrolled = enroll_online(runtime, machine_id="machine_alpha")

    with pytest.raises(ValueError, match="workspace projection must be an object"):
        runtime.heartbeat(
            machine_id="machine_alpha",
            token=enrolled["node_token"],
            edge_generation=enrolled["edge_generation"],
            projection_revision=2,
            workspaces=["malformed"],
        )

    fleet = runtime.fleet_status()["result"]
    assert fleet["machines"][0]["projection_revision"] == 1
    assert len(runtime.workspace_list()["result"]["workspaces"]) == 1


def test_malformed_projection_health_cannot_poison_routing_after_restart(
    tmp_path,
):
    runtime, store, path = make_runtime(tmp_path)
    enrolled = enroll_online(runtime, machine_id="machine_alpha")
    runtime.heartbeat(
        machine_id="machine_alpha",
        token=enrolled["node_token"],
        edge_generation=enrolled["edge_generation"],
        projection_revision=1,
        resource_status={
            "active_workers": 0,
            "free_worker_slots": 4,
            "projection_health": 7,
            "history": "must-not-persist",
        },
    )
    store.close()

    reopened_store = HubStoreV2(path)
    restarted = HubRuntimeV2(
        {
            "hub": {
                "heartbeat_stale_seconds": 90,
                "routing": {"enabled": True, "min_disk_free_bytes": 0},
            }
        },
        reopened_store,
        clock=lambda: 1_001.0,
    )
    routing = restarted.routing_machine_views()
    fleet = restarted.fleet_status()["result"]

    assert routing[0]["workspaces"][0]["local_path"] == "/srv/PatchBay"
    assert routing[0]["projection_health"] == {}
    assert "history" not in routing[0]["resource_status"]
    assert "history" not in json.dumps(fleet)
    reopened_store.close()


def test_fleet_status_sanitizes_raw_edge_fields_and_bounds_workspaces(tmp_path):
    runtime, _, _ = make_runtime(tmp_path)
    enrolled = enroll_online(runtime, machine_id="machine_alpha")
    workspaces = [
        {
            "alias": f"Repo-{index}",
            "path": f"/srv/repos/repo-{index}",
            "exists": True,
            "git": {"is_git_repo": True, "branch": "main", "raw_advertised": "secret" * 10_000},
            "history": "workspace-history" * 10_000,
        }
        for index in range(400)
    ]
    result = runtime.heartbeat(
        machine_id="machine_alpha",
        token=enrolled["node_token"],
        edge_generation=enrolled["edge_generation"],
        projection_revision=2,
        capabilities={
            "contract_hash": HUB_V2_CONTRACT_HASH,
            "max_concurrent_jobs": 25,
            "action_capabilities": {"worker_start": "v1"},
            "raw_report": "capability-history" * 100_000,
        },
        workspaces=workspaces,
        worker_projection={
            "snapshot_kind": "full",
            "complete_worker_set": True,
            "workers": [],
            "tombstones": [],
            "raw_report": "worker-history" * 100_000,
        },
        resource_status={
            "active_workers": 0,
            "max_concurrent_jobs": 25,
            "free_worker_slots": 25,
            "queue_enabled": False,
            "cpu_percent": 5,
            "projection_health": {
                "last_success_at": 999.0,
                "projection_age_seconds": 1.0,
                "raw_report": "projection-history" * 100_000,
            },
            "history": "resource-history" * 100_000,
        },
    )
    assert result["projection_accepted"] is True

    fleet_envelope = runtime.fleet_status(include_workspaces=True)
    fleet = fleet_envelope["result"]
    rendered = json.dumps(fleet, separators=(",", ":"))
    assert len(rendered) < 50_000
    assert "raw_report" not in rendered
    assert "history" not in rendered
    assert "raw_advertised" not in rendered
    machine = fleet["machines"][0]
    assert len(machine["workspaces"]) == 10
    assert machine["workspace_count"] == 400
    assert machine["hidden_workspace_count"] == 390
    assert len(runtime.routing_machine_views()[0]["workspaces"]) == 400
    assert runtime.routing_machine_views()[0]["workspaces"][0]["local_path"]
    validate_hub_v2_tool_output("patchbay_fleet_status", fleet_envelope)


def test_fleet_status_is_bounded_and_routes_detail_to_dedicated_tools(tmp_path):
    runtime, _, _ = make_runtime(tmp_path)
    enrolled = enroll_online(runtime, machine_id="machine_alpha")
    workers = [
        {
            "edge_worker_id": f"wrk_{index}",
            "name": f"Historical worker {index}",
            "turn_state": "completed",
            "liveness": "terminal",
            "report_summary": "large historical report " * 100,
        }
        for index in range(250)
    ]
    heartbeat_workers(runtime, enrolled, 2, workers)
    for index in range(15):
        create_group(runtime, caller=context("owner"), key=f"fleet-group-{index}")

    result = runtime.fleet_status(
        include_workspaces=True, context=context("owner")
    )["result"]
    encoded = json.dumps(result)

    assert len(encoded) < 50_000
    assert "large historical report" not in encoded
    assert "Historical worker" not in encoded
    assert result["machines"][0]["worker_summary"]["counts"]["total"] == 250
    assert "advertised" not in result["machines"][0]["workspaces"][0]
    assert len(result["owned_active_groups"]) == 10
    assert result["owned_active_group_count"] == 15
    assert result["hidden_owned_active_group_count"] == 5
    assert all("worker_refs" not in item for item in result["owned_active_groups"])


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
        resource_status={
            "active_workers": 2,
            "max_concurrent_jobs": 8,
            "free_worker_slots": 6,
            "cpu_percent": 42.0,
            "memory_used_percent": 55.0,
            "disk_free_bytes": 9_000_000_000,
        },
    )

    assert duplicate["projection_accepted"] is False
    assert duplicate["current_projection_revision"] == 1
    assert runtime.fleet_status()["result"]["machines"][0]["compatibility"] == "compatible"
    resources = runtime.fleet_status()["result"]["machines"][0]["resource_status"]
    assert resources["free_worker_slots"] == 6
    assert resources["cpu_percent"] == 42.0
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


def test_workspace_ref_and_child_repo_path_preserve_child_binding(tmp_path):
    runtime, _, _ = make_runtime(tmp_path)
    enroll_online(
        runtime,
        machine_id="machine_root",
        workspace_alias="repos",
        workspace_path="/workspace/repos",
        git=False,
    )
    root_ref = runtime.workspace_list()["result"]["workspaces"][0]["workspace_ref"]

    created = runtime.create_work_group(
        title="Child repository binding",
        goal="Keep the requested repository below the advertised root.",
        workspace_ref=root_ref,
        repo_path="/workspace/repos/child-repo",
        idempotency_key="child-binding-1",
        context=context("owner"),
    )

    group = created["result"]["work_group"]
    assert group["requested_repo_path"] == "/workspace/repos/child-repo"
    assert group["resolved_repo_path"] == "/workspace/repos/child-repo"


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
    assert status["next_actions"] == [
        {
            "tool": "patchbay_operation_status",
            "arguments": {
                "operation_id": preflight_id,
                "wait_seconds": 20,
                "since_revision": status["result"]["dispatch"]["event_revision"],
            },
            "reason": "wait_for_edge_claim",
        }
    ]
    validate_hub_v2_tool_output("patchbay_operation_status", status)

    runtime._clock = lambda: 1_200.0
    degraded = runtime.work_group_status(
        work_group_id=group["work_group_id"], context=context("owner")
    )
    assert degraded["result"]["readiness"]["status"] == "machine_unavailable"
    assert degraded["result"]["work_group"]["pinned_machine_id"] == "machine_free"


def test_group_create_retry_recovers_crash_between_group_and_parent_association(
    tmp_path, monkeypatch
):
    runtime, store, _ = make_runtime(tmp_path)
    enroll_online(runtime, machine_id="machine_group_crash")
    caller = context("group_crash")
    arguments = {
        "title": "Crash-safe group",
        "goal": "Create exactly one durable task object across a retry.",
        "workspace_ref": runtime.workspace_list()["result"]["workspaces"][0][
            "workspace_ref"
        ],
        "machine_id": "machine_group_crash",
        "lanes": [{"lane": "main", "title": "Main", "role": "Build"}],
        "idempotency_key": "group-crash-retry",
        "context": caller,
    }
    original_upsert = runtime._upsert_entity
    crash_once = True

    def crash_before_parent_association(entity_type, entity_id, record):
        nonlocal crash_once
        if crash_once and record.get("kind") == "group_create":
            crash_once = False
            raise RuntimeError("injected post-group crash")
        return original_upsert(entity_type, entity_id, record)

    monkeypatch.setattr(runtime, "_upsert_entity", crash_before_parent_association)
    with pytest.raises(RuntimeError, match="injected post-group crash"):
        runtime.create_work_group(**arguments)

    groups_after_crash = store.list_entities(WORK_GROUP_ENTITY)
    assert len(groups_after_crash) == 1
    durable_group_id = groups_after_crash[0]["entity_id"]

    replayed = runtime.create_work_group(**arguments)

    assert replayed["status"] == "ok"
    assert replayed["result"]["work_group"]["work_group_id"] == durable_group_id
    assert len(store.list_entities(WORK_GROUP_ENTITY)) == 1
    operation_rows = store.connection.execute(
        "SELECT tool, COUNT(*) AS count FROM operations GROUP BY tool"
    ).fetchall()
    counts = {str(row["tool"]): int(row["count"]) for row in operation_rows}
    assert counts["patchbay_work_group_create"] == 1
    assert counts["patchbay_edge_preflight"] == 1
    parent_operation_id = replayed["operation"]["operation_id"]
    association = store.get_entity(OPERATION_GROUP_ENTITY, parent_operation_id)
    assert association is not None
    assert association["record"]["work_group_id"] == durable_group_id


def test_terminal_group_create_replay_does_not_replace_newer_current_group(tmp_path):
    runtime, store, _ = make_runtime(tmp_path)
    enroll_online(runtime, machine_id="machine_alpha")
    caller = context("owner")

    first = create_group(runtime, caller=caller, key="group-first")
    second = create_group(runtime, caller=caller, key="group-second")
    replayed = create_group(runtime, caller=caller, key="group-first")

    assert replayed["result"]["work_group"]["work_group_id"] == first["result"]["work_group"]["work_group_id"]
    current = runtime.list_work_groups(scope="current", context=caller)
    assert current["result"]["work_groups"][0]["work_group_id"] == second["result"]["work_group"]["work_group_id"]
    preflights = store.connection.execute(
        "SELECT COUNT(*) AS count FROM operations WHERE tool = 'patchbay_edge_preflight'"
    ).fetchone()
    assert int(preflights["count"]) == 2


def test_group_reassign_retry_recovers_crash_after_predecessor_supersession(
    tmp_path, monkeypatch
):
    runtime, store, _ = make_runtime(tmp_path)
    enroll_online(runtime, machine_id="machine_reassign_old")
    enroll_online(runtime, machine_id="machine_reassign_new")
    caller = context("reassign_crash")
    created = create_group(
        runtime,
        caller=caller,
        machine_id="machine_reassign_old",
        key="reassign-crash-source",
    )
    predecessor_id = created["result"]["work_group"]["work_group_id"]
    arguments = {
        "work_group_id": predecessor_id,
        "reason": "Move successor work to the other available Edge.",
        "machine_id": "machine_reassign_new",
        "idempotency_key": "reassign-crash-retry",
        "context": caller,
    }
    original_upsert = runtime._upsert_entity
    crash_once = True

    def crash_before_reassign_association(entity_type, entity_id, record):
        nonlocal crash_once
        if crash_once and record.get("kind") == "group_reassign":
            crash_once = False
            raise RuntimeError("injected post-supersession crash")
        return original_upsert(entity_type, entity_id, record)

    monkeypatch.setattr(
        runtime, "_upsert_entity", crash_before_reassign_association
    )
    with pytest.raises(RuntimeError, match="injected post-supersession crash"):
        runtime.reassign_work_group(**arguments)

    groups_after_crash = store.list_entities(WORK_GROUP_ENTITY)
    assert len(groups_after_crash) == 2
    predecessor_after_crash = store.get_entity(WORK_GROUP_ENTITY, predecessor_id)
    assert predecessor_after_crash["record"]["status"] == "superseded"
    durable_successor_id = predecessor_after_crash["record"]["superseded_by"]

    replayed = runtime.reassign_work_group(**arguments)

    assert replayed["status"] == "ok"
    assert replayed["result"]["work_group"]["work_group_id"] == durable_successor_id
    assert len(store.list_entities(WORK_GROUP_ENTITY)) == 2
    operation_rows = store.connection.execute(
        "SELECT tool, COUNT(*) AS count FROM operations GROUP BY tool"
    ).fetchall()
    counts = {str(row["tool"]): int(row["count"]) for row in operation_rows}
    assert counts["patchbay_work_group_reassign"] == 1
    assert counts["patchbay_edge_preflight"] == 2
    reassign_operation = store.connection.execute(
        "SELECT operation_id FROM operations WHERE tool = 'patchbay_work_group_reassign'"
    ).fetchone()
    association = store.get_entity(
        OPERATION_GROUP_ENTITY, str(reassign_operation["operation_id"])
    )
    assert association is not None
    assert association["record"]["work_group_id"] == durable_successor_id


def test_reconciling_operation_recommends_only_the_public_status_tool(tmp_path):
    runtime, store, _ = make_runtime(tmp_path)
    enroll_online(runtime, machine_id="machine_reconcile")
    created = create_group(runtime, caller=context("owner"))
    operation_id = created["result"]["readiness"]["operation_id"]
    operation = store.get_operation(operation_id)
    operation = runtime.broker.transition_operation(
        operation_id,
        expected_revision=int(operation["revision"]),
        state="running",
        principal_ref=str(operation["principal_ref"]),
    )
    operation = runtime.broker.transition_operation(
        operation_id,
        expected_revision=int(operation["revision"]),
        state="outcome_unknown",
        principal_ref=str(operation["principal_ref"]),
    )
    runtime.broker.transition_operation(
        operation_id,
        expected_revision=int(operation["revision"]),
        state="reconciling",
        principal_ref=str(operation["principal_ref"]),
    )

    status = asyncio.run(
        runtime.operation_status(operation_id=operation_id, context=context("owner"))
    )

    assert status["result"]["safe_next_action"] == "wait_for_edge_reconciliation"
    assert status["next_actions"][0]["tool"] == "patchbay_operation_status"
    assert status["next_actions"][0]["reason"] == "wait_for_edge_reconciliation"
    assert all(
        item.get("tool") != "complete_reconciliation"
        for item in status["next_actions"]
    )


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


def test_work_group_defaults_to_end_to_end_completion_contract(tmp_path):
    runtime, store, _ = make_runtime(tmp_path)
    enroll_online(runtime, machine_id="machine_alpha")

    created = create_group(runtime, caller=context("owner"))
    group = created["result"]["work_group"]
    contract = created["result"]["completion_contract"]
    persisted = store.get_entity(WORK_GROUP_ENTITY, group["work_group_id"])["record"]

    assert group["execution_mode"] == "end_to_end"
    assert group["definition_of_done"] == "Coordinate the bounded implementation."
    assert contract["manager_must_continue"] is True
    assert contract["final_response_allowed"] is False
    assert contract["reason"] == "operations_active"
    assert contract["recommended_next_action"]["tool"] == "patchbay_work_group_status"
    assert persisted["execution_mode"] == "end_to_end"
    assert persisted["definition_of_done"] == group["definition_of_done"]


def test_async_handoff_is_explicit_and_allows_a_progress_response(tmp_path):
    runtime, _, _ = make_runtime(tmp_path)
    enroll_online(runtime, machine_id="machine_alpha")

    created = runtime.create_work_group(
        title="Background research",
        goal="Run research and report later.",
        machine_id="machine_alpha",
        execution_mode="asynchronous_handoff",
        definition_of_done="All research reports are complete.",
        idempotency_key="async-handoff-group",
        context=context("owner"),
    )
    group = created["result"]["work_group"]
    contract = created["result"]["completion_contract"]

    assert group["execution_mode"] == "asynchronous_handoff"
    assert group["definition_of_done"] == "All research reports are complete."
    assert contract["manager_must_continue"] is False
    assert contract["final_response_allowed"] is True


def test_work_group_status_waits_for_a_real_revision_change(tmp_path, monkeypatch):
    runtime, store, _ = make_runtime(tmp_path)
    enroll_online(runtime, machine_id="machine_alpha")
    created = create_group(runtime, caller=context("owner"))
    group_id = created["result"]["work_group"]["work_group_id"]
    baseline = runtime.work_group_status(
        work_group_id=group_id,
        context=context("owner"),
    )["result"]["status_revision"]
    full_projection_calls = 0
    revision_probe_calls = 0
    original_status = runtime.work_group_status
    original_revision = store.work_group_status_revision

    def counted_status(**kwargs):
        nonlocal full_projection_calls
        full_projection_calls += 1
        return original_status(**kwargs)

    def counted_revision(work_group_id):
        nonlocal revision_probe_calls
        revision_probe_calls += 1
        return original_revision(work_group_id)

    monkeypatch.setattr(runtime, "work_group_status", counted_status)
    monkeypatch.setattr(store, "work_group_status_revision", counted_revision)

    async def exercise_wait():
        async def mutate_group():
            await asyncio.sleep(0.02)
            entity = store.get_entity(WORK_GROUP_ENTITY, group_id)
            record = dict(entity["record"])
            record["summary"] = "A worker checkpoint arrived."
            store.put_entity(
                WORK_GROUP_ENTITY,
                group_id,
                record,
                expected_revision=entity["revision"],
            )

        mutation = asyncio.create_task(mutate_group())
        result = await runtime.handle_tool_call(
            "patchbay_work_group_status",
            {
                "work_group_id": group_id,
                "since_revision": baseline,
                "wait_for_change_seconds": 1,
            },
            context=context("owner"),
        )
        await mutation
        return result

    waited = asyncio.run(exercise_wait())

    assert waited["result"]["changed"] is True
    assert waited["result"]["waited_seconds"] > 0
    assert waited["result"]["status_revision"] > baseline
    assert waited["result"]["completion_contract"]["final_response_allowed"] is False
    assert revision_probe_calls >= 1
    assert full_projection_calls == 2


def test_work_group_status_truthfully_pages_integration_dispositions(tmp_path):
    runtime, _, _ = make_runtime(tmp_path)
    enrolled = enroll_online(runtime, machine_id="machine_alpha")
    caller = context("owner")
    created = create_group(runtime, caller=caller)
    group_id = created["result"]["work_group"]["work_group_id"]
    workers = [
        {
            "edge_worker_id": "worker-implementer",
            "name": "Implementer",
            "work_group_id": group_id,
            "lane_id": "implementation",
            "turn_state": "completed",
            "liveness": "terminal",
            "integration_state": "not_integrated",
            "review_disposition": "accepted",
        },
        {
            "edge_worker_id": "worker-reviewer",
            "name": "Reviewer",
            "work_group_id": group_id,
            "lane_id": "verification",
            "turn_state": "completed",
            "liveness": "terminal",
            "integration_state": "no_changes",
            "review_disposition": "approved",
        },
    ]
    heartbeat_workers(runtime, enrolled, 2, workers)

    first = runtime.work_group_status(
        work_group_id=group_id,
        worker_limit=1,
        integration_limit=1,
        context=caller,
    )

    assert first["result"]["worker_summary"]["total"] == 2
    assert first["result"]["worker_summary"]["unintegrated"] == 1
    assert first["result"]["worker_page"]["next_cursor"] == "1"
    assert first["result"]["integration_summary"] == {
        "total": 2,
        "state_counts": {"no_changes": 1, "not_integrated": 1},
        "review_disposition_counts": {"accepted": 1, "approved": 1},
    }
    assert len(first["result"]["integrations"]) == 1
    assert first["result"]["integration_page"]["next_cursor"] == "1"
    assert first["result"]["work_group"]["worker_count"] == 2
    assert first["result"]["work_group"]["worker_refs_truncated"] is True

    second = runtime.work_group_status(
        work_group_id=group_id,
        integration_cursor="1",
        integration_limit=1,
        context=caller,
    )
    assert second["result"]["integration_page"]["cursor"] == "1"
    assert second["result"]["integration_page"]["next_cursor"] == ""
    assert second["result"]["integrations"] != first["result"]["integrations"]

    hidden = runtime.work_group_status(
        work_group_id=group_id,
        include_integrations=False,
        context=caller,
    )
    assert "integrations" not in hidden["result"]
    assert "integration_summary" not in hidden["result"]
    assert "integration_page" not in hidden["result"]


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


def test_preflight_records_snapshot_revision_and_observation_time(tmp_path):
    runtime, store, _ = make_runtime(tmp_path)
    enroll_online(runtime, machine_id="machine_alpha")
    created = create_group(runtime, caller=context("owner"))
    group = created["result"]["work_group"]
    result = runtime.record_preflight_result(
        work_group_id=group["work_group_id"],
        operation_id=created["result"]["readiness"]["operation_id"],
        result={
            "ok": True,
            "repo_exists": True,
            "repo_resolved": group["resolved_repo_path"],
            "head_revision": "abc123",
            "disk_free_bytes": 10_000_000_000,
            "free_worker_slots": 2,
        },
    )
    readiness = result["result"]["readiness"]
    assert readiness["currentness"] == "current"
    assert readiness["facts_revision"] == "abc123"
    assert readiness["observed_at"] == readiness["updated_at"]


def test_base_mutation_marks_preflight_snapshot_refresh_required_without_blocking_group(tmp_path):
    runtime, store, _ = make_runtime(tmp_path)
    enroll_online(runtime, machine_id="machine_alpha")
    created = create_group(runtime, caller=context("owner"))
    group = created["result"]["work_group"]
    runtime.record_preflight_result(
        work_group_id=group["work_group_id"],
        operation_id=created["result"]["readiness"]["operation_id"],
        result={
            "ok": True,
            "repo_exists": True,
            "repo_resolved": group["resolved_repo_path"],
            "head": "abc123",
            "disk_free_bytes": 10_000_000_000,
            "free_worker_slots": 2,
        },
    )

    updated = runtime.mark_group_preflight_refresh_required(
        work_group_id=group["work_group_id"],
        reason="accepted_worker_integration_changed_base_checkout",
        source_operation_id="op-integrate",
    )

    readiness = updated["readiness"]
    assert readiness["status"] == "ready"
    assert readiness["currentness"] == "refresh_required"
    assert readiness["facts_revision"] == "abc123"
    assert readiness["stale_source_operation_id"] == "op-integrate"
    persisted = store.get_entity(WORK_GROUP_ENTITY, group["work_group_id"])["record"]
    assert persisted["readiness"]["currentness"] == "refresh_required"


def test_group_persists_architect_selected_shared_write_policy(tmp_path):
    runtime, _, _ = make_runtime(tmp_path)
    enroll_online(runtime, machine_id="machine_alpha")

    created = runtime.create_work_group(
        title="Concurrent checkout writers",
        goal="Let the architect coordinate compatible shared writers.",
        machine_id="machine_alpha",
        shared_write_policy="manager_controlled",
        idempotency_key="group-shared-manager-controlled",
        context=context("owner"),
    )

    assert created["result"]["work_group"]["shared_write_policy"] == "manager_controlled"


def test_completed_base_mutation_reconciles_current_group_snapshot(tmp_path):
    runtime, _, _ = make_runtime(tmp_path)
    enroll_online(runtime, machine_id="machine_alpha")
    created = create_group(runtime, caller=context("owner"))
    group = created["result"]["work_group"]
    runtime.record_preflight_result(
        work_group_id=group["work_group_id"],
        operation_id=created["result"]["readiness"]["operation_id"],
        result={
            "ok": True,
            "repo_exists": True,
            "repo_resolved": group["resolved_repo_path"],
            "head": "abc123",
            "disk_free_bytes": 10_000_000_000,
            "free_worker_slots": 2,
        },
    )

    refreshed = runtime.record_group_base_mutation_snapshot(
        work_group_id=group["work_group_id"],
        snapshot={
            "head": "abc123",
            "changed_files": ["generated.txt"],
            "dirty": True,
            "observed_at": 1234.0,
        },
        reason="accepted_worker_integration_changed_base_checkout",
        source_operation_id="op-integrate-refresh",
    )

    readiness = refreshed["readiness"]
    assert readiness["status"] == "ready"
    assert readiness["currentness"] == "current"
    assert readiness["facts"]["git"]["dirty"] is True
    assert readiness["facts"]["git"]["status_short"] == ["generated.txt"]
    assert readiness["mutation_source_operation_id"] == "op-integrate-refresh"


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


def test_terminal_group_resume_replay_preserves_completed_preflight(tmp_path):
    runtime, store, _ = make_runtime(tmp_path)
    enroll_online(runtime, machine_id="machine_alpha")
    caller = context("owner")
    created = create_group(runtime, caller=caller)
    group_id = created["result"]["work_group"]["work_group_id"]
    arguments = {
        "work_group_id": group_id,
        "idempotency_key": "resume-ready-replay",
        "context": caller,
    }

    resumed = runtime.resume_work_group(**arguments)
    preflight_id = resumed["result"]["readiness"]["operation_id"]
    runtime.record_preflight_result(
        work_group_id=group_id,
        operation_id=preflight_id,
        result={
            "ok": True,
            "repo_exists": True,
            "repo_resolved": resumed["result"]["work_group"]["resolved_repo_path"],
            "head": "resume-ready-head",
            "disk_free_bytes": 10_000_000_000,
            "free_worker_slots": 4,
        },
    )
    replayed = runtime.resume_work_group(**arguments)

    assert replayed["status"] == "ok"
    assert replayed["result"]["readiness"]["status"] == "ready"
    assert replayed["result"]["readiness"]["operation_id"] == preflight_id
    preflights = store.connection.execute(
        "SELECT COUNT(*) AS count FROM operations WHERE tool = 'patchbay_edge_preflight'"
    ).fetchone()
    assert int(preflights["count"]) == 2


def test_handle_tool_call_returns_structured_idempotency_conflict(tmp_path):
    runtime, store, _ = make_runtime(tmp_path)
    enroll_online(runtime, machine_id="machine_alpha")
    caller = context("owner")
    created = create_group(runtime, caller=caller)
    group_id = created["result"]["work_group"]["work_group_id"]
    first = asyncio.run(
        runtime.handle_tool_call(
            "patchbay_work_group_resume",
            {"work_group_id": group_id, "idempotency_key": "resume-stable-key"},
            context=caller,
        )
    )
    conflict = asyncio.run(
        runtime.handle_tool_call(
            "patchbay_work_group_resume",
            {
                "work_group_id": group_id,
                "takeover": True,
                "takeover_reason": "Changed semantic request.",
                "idempotency_key": "resume-stable-key",
            },
            context=caller,
        )
    )

    assert first["status"] == "ok"
    assert conflict["status"] == "blocked"
    assert conflict["result"] == {
        "reason": "idempotency_payload_conflict",
        "retry_safe": False,
    }
    assert "exact original arguments" in conflict["next_actions"][0]
    validate_hub_v2_tool_output("patchbay_work_group_resume", conflict)
    store.close()


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
    operations_before_refusal = store.operation_ids_for_work_group(group_id)

    refused = runtime.close_work_group(
        work_group_id=group_id,
        outcome="complete",
        summary="Premature close.",
        worker_dispositions={worker_ref: "no_changes"},
        active_work_disposition="refuse",
        idempotency_key="close-refused",
        context=caller,
    )
    assert refused["status"] == "blocked"
    assert refused["result"]["reason"] == "close_disposition_refused"
    assert store.operation_ids_for_work_group(group_id) == operations_before_refusal

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

    assert closed["status"] == "ok"
    assert closed["result"]["work_group"]["status"] == "closed"
    assert runtime.list_work_groups(scope="current", context=caller)["result"]["work_groups"] == []


def test_group_close_retry_recovers_crash_after_group_is_persisted(tmp_path, monkeypatch):
    runtime, store, _ = make_runtime(tmp_path)
    enroll_online(runtime, machine_id="machine_alpha")
    caller = context("owner")
    created = create_group(runtime, caller=caller)
    group_id = created["result"]["work_group"]["work_group_id"]
    original_complete = runtime._complete_hub_operation

    def fail_after_close_state(*args, **kwargs):
        raise RuntimeError("injected close completion crash")

    monkeypatch.setattr(runtime, "_complete_hub_operation", fail_after_close_state)
    with pytest.raises(RuntimeError, match="injected close completion crash"):
        runtime.close_work_group(
            work_group_id=group_id,
            outcome="complete",
            summary="All work is complete.",
            worker_dispositions={},
            idempotency_key="close-crash-recovery",
            context=caller,
        )

    persisted = store.get_entity(WORK_GROUP_ENTITY, group_id)
    assert persisted is not None
    assert persisted["record"]["status"] == "closed"
    close_associations = [
        item
        for item in store.list_entities(OPERATION_GROUP_ENTITY)
        if item["record"].get("kind") == "group_close"
        and item["record"].get("work_group_id") == group_id
    ]
    assert len(close_associations) == 1
    operation_id = close_associations[0]["record"]["operation_id"]
    assert store.get_operation(operation_id)["state"] not in {"succeeded", "blocked"}

    monkeypatch.setattr(runtime, "_complete_hub_operation", original_complete)
    recovered = runtime.close_work_group(
        work_group_id=group_id,
        outcome="complete",
        summary="All work is complete.",
        worker_dispositions={},
        idempotency_key="close-crash-recovery",
        context=caller,
    )

    assert recovered["status"] == "ok"
    assert recovered["operation"]["operation_id"] == operation_id
    assert store.get_operation(operation_id)["state"] == "succeeded"
    assert len(store.list_entities(WORK_GROUP_ENTITY)) == 1
    assert runtime.list_work_groups(scope="current", context=caller)["result"]["work_groups"] == []


def test_group_close_retry_excludes_only_its_own_running_hub_operation(tmp_path):
    runtime, store, _ = make_runtime(tmp_path)
    enroll_online(runtime, machine_id="machine_alpha")
    caller = context("owner")
    principal_ref = runtime._manager_identity(caller).principal_ref
    created = create_group(runtime, caller=caller)
    group_id = created["result"]["work_group"]["work_group_id"]
    operation = runtime.broker.create_operation(
        tool="patchbay_work_group_close",
        logical_target=group_id,
        idempotency_key="close-running-recovery",
        payload={
            "work_group_id": group_id,
            "outcome": "complete",
            "summary": "Recover the accepted close.",
            "worker_dispositions": {},
            "active_work_disposition": "refuse",
        },
        principal_ref=principal_ref,
    )
    runtime.broker.associate_operation(
        operation["operation_id"],
        work_group_id=group_id,
        principal_ref=principal_ref,
        kind="group_close",
    )
    for state in ("payload_ready", "dispatchable", "running"):
        operation = runtime.broker.transition_operation(
            operation["operation_id"],
            expected_revision=operation["revision"],
            state=state,
        )
    operation_ids = store.operation_ids_for_work_group(group_id)

    unrelated_retry = runtime.close_work_group(
        work_group_id=group_id,
        outcome="complete",
        summary="A different close request.",
        worker_dispositions={},
        idempotency_key="close-different-key",
        context=caller,
    )
    assert unrelated_retry["status"] == "blocked"
    assert store.operation_ids_for_work_group(group_id) == operation_ids

    recovered = runtime.close_work_group(
        work_group_id=group_id,
        outcome="complete",
        summary="Recover the accepted close.",
        worker_dispositions={},
        idempotency_key="close-running-recovery",
        context=caller,
    )
    assert recovered["status"] == "ok"
    assert recovered["operation"]["operation_id"] == operation["operation_id"]
    assert store.get_operation(operation["operation_id"])["state"] == "succeeded"
    assert recovered["result"]["work_group"]["status"] == "closed"


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


def test_close_records_manager_review_of_failed_worker_without_private_edge_flag(tmp_path):
    runtime, store, _ = make_runtime(tmp_path)
    enrolled = enroll_online(runtime, machine_id="machine_alpha")
    caller = context("owner")
    created = create_group(runtime, caller=caller)
    group_id = created["result"]["work_group"]["work_group_id"]
    failed = {
        "edge_worker_id": "worker-failed",
        "name": "Failed Investigator",
        "work_group_id": group_id,
        "lane_id": "investigation",
        "worker_state": "available",
        "turn_state": "failed",
        "liveness": "terminal",
        "integration_state": "no_changes",
        "review_disposition": "unreviewed",
    }
    heartbeat_workers(runtime, enrolled, 2, [failed])
    worker_ref = store.list_entities(FLEET_WORKER_ENTITY)[0]["entity_id"]

    closed = runtime.close_work_group(
        work_group_id=group_id,
        outcome="complete",
        summary="The manager reviewed the failed advisory lane and accepted its absence.",
        worker_dispositions={worker_ref: "reviewed_failure"},
        idempotency_key="close-reviewed-failure",
        context=caller,
    )

    assert closed["status"] == "ok"
    assert closed["result"]["work_group"]["closure_dispositions"] == {
        worker_ref: "reviewed_failure"
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


def test_explicit_operation_group_association_overrides_logical_target(tmp_path):
    runtime, store, _ = make_runtime(tmp_path)
    fallback = runtime.broker.create_operation(
        tool="patchbay_worker_status",
        logical_target="group-predecessor",
        idempotency_key="fallback-operation",
        payload={"work_group_id": "group-predecessor"},
    )
    reassignment = runtime.broker.create_operation(
        tool="patchbay_work_group_reassign",
        logical_target="group-predecessor",
        idempotency_key="reassignment-operation",
        payload={"work_group_id": "group-predecessor"},
    )
    store.put_entity(
        OPERATION_GROUP_ENTITY,
        reassignment["operation_id"],
        {
            "operation_id": reassignment["operation_id"],
            "work_group_id": "group-successor",
            "kind": "group_reassign",
        },
        expected_revision=0,
    )

    predecessor_ids = {
        operation["operation_id"]
        for operation in runtime._operations_for_group("group-predecessor")
    }
    successor_ids = {
        operation["operation_id"]
        for operation in runtime._operations_for_group("group-successor")
    }

    assert fallback["operation_id"] in predecessor_ids
    assert reassignment["operation_id"] not in predecessor_ids
    assert reassignment["operation_id"] in successor_ids
    assert fallback["operation_id"] not in successor_ids
    store.close()
