from pathlib import Path

from patchbay.hub.runtime import HubRuntime
from patchbay.hub.store import HubStore


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
