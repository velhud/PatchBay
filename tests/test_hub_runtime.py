from pathlib import Path

import pytest

from patchbay.hub.runtime import HubRuntime
from patchbay.hub.store import HubStore, HubStoreCorrupt
from patchbay.protocol.context import RequestContext


def hub_config(tmp_path: Path, *, routing_enabled: bool = False):
    return {
        "hub": {
            "state_file": str(tmp_path / "hub-state.json"),
            "heartbeat_stale_seconds": 90,
            "routing": {
                "enabled": routing_enabled,
                "min_disk_free_bytes": 1024,
                "allow_queue_when_full": False,
                "weights": {"worker_ratio": 0.60, "memory_ratio": 0.20, "cpu_ratio": 0.20},
            },
        },
        "server": {"max_concurrent_jobs": 3, "queue_enabled": True},
        "repositories": {"default": str(tmp_path), "allowed": [str(tmp_path)]},
        "security": {"default_sandbox": "danger-full-access"},
        "power_tools": {"direct_write": True, "bash_mode": "full"},
        "logging": {
            "audit_file": str(tmp_path / "logs" / "audit.log"),
            "job_logs_dir": str(tmp_path / "logs" / "jobs"),
            "job_state_dir": str(tmp_path / "logs" / "jobs" / "state"),
        },
    }


def enroll_online(runtime: HubRuntime, machine_id: str, *, token_name: str | None = None, tags=None, resources=None):
    code = runtime.create_enrollment_code(name=machine_id)["code"]
    enrolled = runtime.enroll_machine(code=code, machine_id=machine_id, display_name=machine_id, tags=tags or [])
    token = enrolled["node_token"]
    runtime.heartbeat(
        machine_id=machine_id,
        token=token,
        capabilities={"codex_worker_tools": True, "max_concurrent_jobs": 4, "queue_enabled": True},
        worker_status={"worker_lines": []},
        resource_status={
            "active_workers": 0,
            "max_concurrent_jobs": 4,
            "free_worker_slots": 4,
            "queue_enabled": True,
            "cpu_percent": 10,
            "memory_used_percent": 10,
            "memory_available_bytes": 8_000_000_000,
            "disk_free_bytes": 10_000_000_000,
            "disk_used_percent": 20,
            **(resources or {}),
        },
    )
    return token_name or token


def complete_next_preflight(runtime: HubRuntime, *, machine_id: str, token: str):
    claimed = runtime.claim_next_command(machine_id=machine_id, token=token)
    command = claimed["command"]
    assert command["action"] == "patchbay_edge_preflight"
    return runtime.finish_command(
        machine_id=machine_id,
        token=token,
        command_id=command["command_id"],
        result={"ok": True, "repo_exists": True, "git_repo": True, "branch": "main", "head": "abc123"},
    )


def test_hub_enrollment_stores_only_token_hash(tmp_path):
    runtime = HubRuntime(hub_config(tmp_path))

    code = runtime.create_enrollment_code(name="Dev Mac", tags=["local"])["code"]
    result = runtime.enroll_machine(
        code=code,
        machine_id="dev-mac",
        display_name="Dev Mac",
        tags=["local"],
        capabilities={"codex": True},
        workspaces=[{"alias": "PatchBay", "path": "/tmp/PatchBay"}],
    )

    assert result["machine"]["machine_id"] == "dev-mac"
    assert result["node_token"].startswith("node_")
    stored = HubStore(hub_config(tmp_path)).read()["machines"]["dev-mac"]
    assert stored["token_hash"]
    assert result["node_token"] not in str(stored)
    assert "node_token" not in runtime.list_machines()["machines"][0]


def test_hub_heartbeat_command_queue_and_finish(tmp_path):
    runtime = HubRuntime(hub_config(tmp_path))
    code = runtime.create_enrollment_code(name="VM")["code"]
    enrolled = runtime.enroll_machine(code=code, machine_id="ucl-vm", display_name="ucl-vm")
    token = enrolled["node_token"]

    heartbeat = runtime.heartbeat(
        machine_id="ucl-vm",
        token=token,
        worker_status={"worker_lines": ["worker A: running"]},
    )
    assert heartbeat["accepted"] is True
    assert runtime.fleet_status()["active_workers"][0]["machine_id"] == "ucl-vm"

    queued = runtime.create_command(
        machine_id="ucl-vm",
        action="codex_worker_start",
        arguments={"name": "Architect", "brief": "Inspect the repo."},
    )
    assert queued["state"] == "queued"
    assert "arguments" not in queued

    claimed = runtime.claim_next_command(machine_id="ucl-vm", token=token)
    assert claimed["command"]["command_id"] == queued["command_id"]
    assert claimed["command"]["arguments"]["name"] == "Architect"

    finished = runtime.finish_command(
        machine_id="ucl-vm",
        token=token,
        command_id=queued["command_id"],
        result={"accepted": True},
    )
    assert finished["state"] == "completed"
    assert runtime.command_status(command_id=queued["command_id"])["commands"][0]["state"] == "completed"


def test_hub_routing_disabled_returns_disabled_response(tmp_path):
    runtime = HubRuntime(hub_config(tmp_path))

    result = runtime.recommend_machine()

    assert result["enabled"] is False
    assert result["selected_machine_id"] == ""
    assert "explicit machine_id" in result["recommended_next_action"]


def test_hub_routing_rejects_offline_machine(tmp_path):
    runtime = HubRuntime(hub_config(tmp_path, routing_enabled=True))
    code = runtime.create_enrollment_code(name="offline")["code"]
    runtime.enroll_machine(code=code, machine_id="offline", display_name="offline", capabilities={"codex_worker_tools": True})
    enroll_online(runtime, "online")

    result = runtime.recommend_machine()

    assert result["selected_machine_id"] == "online"
    rejected = {candidate["machine_id"]: candidate["rejected_reasons"] for candidate in result["rejected_candidates"]}
    assert "offline" in rejected["offline"]


def test_hub_routing_lower_worker_ratio_wins(tmp_path):
    runtime = HubRuntime(hub_config(tmp_path, routing_enabled=True))
    enroll_online(runtime, "less-busy", resources={"active_workers": 1, "free_worker_slots": 3})
    enroll_online(runtime, "more-busy", resources={"active_workers": 3, "free_worker_slots": 1})

    result = runtime.recommend_machine()

    assert result["selected_machine_id"] == "less-busy"
    assert result["ranked_candidates"][0]["score_reasons"]["worker_ratio"] == 0.25


def test_hub_routing_cpu_and_memory_change_ranking_when_worker_ratio_close(tmp_path):
    runtime = HubRuntime(hub_config(tmp_path, routing_enabled=True))
    enroll_online(
        runtime,
        "cool",
        resources={"active_workers": 1, "free_worker_slots": 3, "cpu_percent": 10, "memory_used_percent": 20},
    )
    enroll_online(
        runtime,
        "hot",
        resources={"active_workers": 1, "free_worker_slots": 3, "cpu_percent": 80, "memory_used_percent": 80},
    )

    result = runtime.recommend_machine()

    assert result["selected_machine_id"] == "cool"
    assert result["ranked_candidates"][0]["score"] < result["ranked_candidates"][1]["score"]


def test_hub_routing_critically_low_disk_rejects_machine(tmp_path):
    runtime = HubRuntime(hub_config(tmp_path, routing_enabled=True))
    enroll_online(runtime, "full-disk", resources={"disk_free_bytes": 1, "disk_used_percent": 99, "active_workers": 0, "free_worker_slots": 4})
    enroll_online(runtime, "available", resources={"active_workers": 2, "free_worker_slots": 2})

    result = runtime.recommend_machine()

    assert result["selected_machine_id"] == "available"
    rejected = {candidate["machine_id"]: candidate["rejected_reasons"] for candidate in result["rejected_candidates"]}
    assert "disk free below routing minimum" in rejected["full-disk"]


def test_hub_routing_tie_breakers_are_stable(tmp_path):
    runtime = HubRuntime(hub_config(tmp_path, routing_enabled=True))
    enroll_online(runtime, "bravo")
    enroll_online(runtime, "alpha")

    result = runtime.recommend_machine()

    assert [candidate["machine_id"] for candidate in result["ranked_candidates"][:2]] == ["alpha", "bravo"]


def test_hub_routing_required_tags_filter_candidates(tmp_path):
    runtime = HubRuntime(hub_config(tmp_path, routing_enabled=True))
    enroll_online(runtime, "untagged")
    enroll_online(runtime, "laptop", tags=["portable"])

    result = runtime.recommend_machine(required_tags=["portable"])

    assert result["selected_machine_id"] == "laptop"
    rejected = {candidate["machine_id"]: candidate["rejected_reasons"] for candidate in result["rejected_candidates"]}
    assert any("missing required tags" in reason for reason in rejected["untagged"])


def test_hub_work_group_create_persists_and_queues_preflight(tmp_path):
    runtime = HubRuntime(hub_config(tmp_path, routing_enabled=True))
    token = enroll_online(runtime, "edge")

    created = runtime.create_work_group(title="RetailMind stage", goal="Plan next implementation", repo_path=str(tmp_path))

    group = created["work_group"]
    assert created["accepted"] is True
    assert group["pinned_machine_id"] == "edge"
    assert group["preflight"]["status"] == "pending"
    assert created["preflight_command"]["action"] == "patchbay_edge_preflight"

    restarted = HubRuntime(hub_config(tmp_path, routing_enabled=True))
    listed = restarted.list_work_groups(scope="history", include_closed=True)
    assert listed["groups"][0]["work_group_id"] == group["work_group_id"]
    claimed = restarted.claim_next_command(machine_id="edge", token=token)
    assert claimed["command"]["work_group_id"] == group["work_group_id"]


def test_hub_work_group_idempotency_key_reuses_same_group(tmp_path):
    runtime = HubRuntime(hub_config(tmp_path, routing_enabled=True))
    enroll_online(runtime, "edge")

    first = runtime.create_work_group(title="Task", goal="Do the thing", idempotency_key="same-key")
    second = runtime.create_work_group(title="Task retry", goal="Do the thing again", idempotency_key="same-key")

    assert second["idempotent_replay"] is True
    assert first["work_group"]["work_group_id"] == second["work_group"]["work_group_id"]


def test_hub_auto_worker_start_requires_ok_preflight(tmp_path):
    runtime = HubRuntime(hub_config(tmp_path, routing_enabled=True))
    enroll_online(runtime, "edge")
    created = runtime.create_work_group(title="Task", goal="Do the thing")
    group_id = created["work_group"]["work_group_id"]

    with pytest.raises(ValueError, match="preflight"):
        runtime.queue_auto_worker_start(
            arguments={
                "work_group_id": group_id,
                "lane": "reader",
                "auto_routing_ok": True,
                "name": "Reader",
                "brief": "Read docs.",
            }
        )


def test_hub_grouped_auto_start_stays_on_pinned_machine_after_load_changes(tmp_path):
    runtime = HubRuntime(hub_config(tmp_path, routing_enabled=True))
    pinned_token = enroll_online(runtime, "pinned", resources={"active_workers": 0, "free_worker_slots": 4})
    other_token = enroll_online(runtime, "other", resources={"active_workers": 2, "free_worker_slots": 2})
    created = runtime.create_work_group(title="Task", goal="Do the thing")
    group_id = created["work_group"]["work_group_id"]
    assert created["work_group"]["pinned_machine_id"] == "pinned"
    complete_next_preflight(runtime, machine_id="pinned", token=pinned_token)

    runtime.heartbeat(
        machine_id="pinned",
        token=pinned_token,
        capabilities={"codex_worker_tools": True, "max_concurrent_jobs": 4, "queue_enabled": True},
        resource_status={
            "active_workers": 3,
            "max_concurrent_jobs": 4,
            "free_worker_slots": 1,
            "queue_enabled": True,
            "cpu_percent": 90,
            "memory_used_percent": 90,
            "memory_available_bytes": 1_000_000_000,
            "disk_free_bytes": 10_000_000_000,
        },
    )
    runtime.heartbeat(
        machine_id="other",
        token=other_token,
        capabilities={"codex_worker_tools": True, "max_concurrent_jobs": 4, "queue_enabled": True},
        resource_status={
            "active_workers": 0,
            "max_concurrent_jobs": 4,
            "free_worker_slots": 4,
            "queue_enabled": True,
            "cpu_percent": 5,
            "memory_used_percent": 10,
            "memory_available_bytes": 9_000_000_000,
            "disk_free_bytes": 10_000_000_000,
        },
    )

    queued = runtime.queue_auto_worker_start(
        arguments={
            "work_group_id": group_id,
            "lane": "reader",
            "auto_routing_ok": True,
            "name": "Reader",
            "brief": "Read docs.",
        }
    )

    assert queued["accepted"] is True
    assert queued["machine_id"] == "pinned"
    assert queued["routing"]["selected_machine_id"] == "pinned"
    claimed = runtime.claim_next_command(machine_id="pinned", token=pinned_token)
    assert claimed["command"]["action"] == "codex_worker_start"
    runtime.finish_command(
        machine_id="pinned",
        token=pinned_token,
        command_id=claimed["command"]["command_id"],
        result={"worker_id": "wrk_reader", "name": "Reader"},
    )
    group_status = runtime.work_group_status(work_group_id=group_id)
    assert group_status["work_group"]["worker_refs"][0]["worker_id"] == "wrk_reader"


def test_hub_group_recommend_blocks_when_pinned_machine_offline(tmp_path):
    runtime = HubRuntime(hub_config(tmp_path, routing_enabled=True))
    token = enroll_online(runtime, "edge")
    created = runtime.create_work_group(title="Task", goal="Do the thing")
    group_id = created["work_group"]["work_group_id"]
    complete_next_preflight(runtime, machine_id="edge", token=token)

    payload = runtime.store.read()
    payload["machines"]["edge"]["last_seen_at"] = 1
    runtime.store._write(payload)

    recommendation = runtime.recommend_machine(work_group_id=group_id)

    assert recommendation["selected_machine_id"] == ""
    assert recommendation["blocked_reason"] == "pinned_machine_not_eligible"


def test_hub_ungrouped_worker_start_requires_reason(tmp_path):
    runtime = HubRuntime(hub_config(tmp_path, routing_enabled=True))
    enroll_online(runtime, "edge")

    with pytest.raises(ValueError, match="ungrouped_reason"):
        runtime.queue_worker_command(
            machine_id="edge",
            action="codex_worker_start",
            arguments={"name": "Loose", "brief": "Run loose."},
        )

    queued = runtime.queue_worker_command(
        machine_id="edge",
        action="codex_worker_start",
        arguments={"name": "Loose", "brief": "Run loose.", "ungrouped_reason": "tiny_check"},
        ungrouped_reason="tiny_check",
    )
    assert queued["state"] == "queued"
    assert queued["routing"]["ungrouped_reason"] == "tiny_check"


def test_hub_command_status_filters_by_work_group(tmp_path):
    runtime = HubRuntime(hub_config(tmp_path, routing_enabled=True))
    enroll_online(runtime, "edge")
    created = runtime.create_work_group(title="Task", goal="Do the thing")
    group_id = created["work_group"]["work_group_id"]

    status = runtime.command_status(work_group_id=group_id)

    assert status["count"] == 1
    assert status["commands"][0]["work_group_id"] == group_id
    assert "arguments" not in status["commands"][0]


def test_hub_context_refs_reach_grouped_edge_command(tmp_path):
    runtime = HubRuntime(hub_config(tmp_path, routing_enabled=True))
    token = enroll_online(runtime, "edge")
    context = RequestContext(
        client_ref="client_123",
        owner_ref="owner_123",
        chatgpt_session_ref="chatgpt_session_123",
        work_run_ref="run_123",
    )
    created = runtime.create_work_group(title="Task", goal="Do the thing", context=context)
    group_id = created["work_group"]["work_group_id"]
    complete_next_preflight(runtime, machine_id="edge", token=token)

    queued = runtime.queue_auto_worker_start(
        arguments={
            "work_group_id": group_id,
            "lane": "reader",
            "auto_routing_ok": True,
            "name": "Reader",
            "brief": "Read docs.",
        },
        context=context,
    )
    claimed = runtime.claim_next_command(machine_id="edge", token=token)["command"]

    assert queued["work_group_id"] == group_id
    assert claimed["context"]["chatgpt_session_ref"] == "chatgpt_session_123"
    assert claimed["context"]["work_run_ref"] == "run_123"
    assert claimed["context"]["work_group_id"] == group_id
    assert claimed["context"]["lane_id"] == "reader"


def test_hub_work_group_close_refuses_active_commands_by_default(tmp_path):
    runtime = HubRuntime(hub_config(tmp_path, routing_enabled=True))
    enroll_online(runtime, "edge")
    created = runtime.create_work_group(title="Task", goal="Do the thing")
    group_id = created["work_group"]["work_group_id"]

    refused = runtime.close_work_group(work_group_id=group_id, outcome="complete", summary="Done")

    assert refused["accepted"] is False
    assert refused["active_commands"][0]["action"] == "patchbay_edge_preflight"
    closed = runtime.close_work_group(work_group_id=group_id, outcome="complete", summary="Done", force=True)
    assert closed["accepted"] is True
    assert closed["work_group"]["status"] == "complete"


def test_hub_work_group_close_clears_current_group_pointer(tmp_path):
    runtime = HubRuntime(hub_config(tmp_path, routing_enabled=True))
    enroll_online(runtime, "edge")
    context = RequestContext(chatgpt_session_ref="session-a")
    created = runtime.create_work_group(title="Task", goal="Do the thing", context=context)
    group_id = created["work_group"]["work_group_id"]

    assert runtime.list_work_groups(scope="recent", context=context)["current_work_group_id"] == group_id

    closed = runtime.close_work_group(work_group_id=group_id, outcome="complete", summary="Done", force=True, context=context)

    assert closed["accepted"] is True
    listed = runtime.list_work_groups(scope="recent", include_closed=False, context=context)
    assert listed["groups"] == []
    assert listed["current_work_group_id"] == ""


def test_hub_work_group_reassign_supersedes_old_lanes_and_queues_preflight(tmp_path):
    runtime = HubRuntime(hub_config(tmp_path, routing_enabled=True))
    token_a = enroll_online(runtime, "a")
    token_b = enroll_online(runtime, "b", resources={"active_workers": 1, "free_worker_slots": 3})
    created = runtime.create_work_group(title="Task", goal="Do the thing", machine_id="a", lanes=["reader"])
    group_id = created["work_group"]["work_group_id"]
    complete_next_preflight(runtime, machine_id="a", token=token_a)

    reassigned = runtime.reassign_work_group(work_group_id=group_id, machine_id="b", reason="Use the other machine")

    assert reassigned["accepted"] is True
    assert reassigned["work_group"]["pinned_machine_id"] == "b"
    assert reassigned["work_group"]["lanes"]["reader"]["status"] == "superseded"
    assert reassigned["successor_lane_id"] in reassigned["work_group"]["lanes"]
    claimed = runtime.claim_next_command(machine_id="b", token=token_b)
    assert claimed["command"]["action"] == "patchbay_edge_preflight"
    assert claimed["command"]["work_group_id"] == group_id


def test_hub_store_corruption_is_quarantined_not_reset(tmp_path):
    config = hub_config(tmp_path)
    store = HubStore(config)
    store.path.write_text("{not json", encoding="utf-8")

    with pytest.raises(HubStoreCorrupt):
        store.read()

    assert list(tmp_path.glob("hub-state.json.corrupt.*"))
