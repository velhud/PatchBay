import asyncio
from pathlib import Path

from patchbay.hub.protocol import HubProtocol
from patchbay.hub.runtime import HubRuntime


def hub_config(tmp_path: Path, *, routing_enabled: bool = False):
    return {
        "hub": {
            "state_file": str(tmp_path / "hub-state.json"),
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


def call(protocol, name, arguments=None):
    response = asyncio.run(
        protocol.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )
    )
    return response["result"]["structuredContent"]


def test_hub_initialize_and_tool_list(tmp_path):
    protocol = HubProtocol(HubRuntime(hub_config(tmp_path)))

    initialized = asyncio.run(
        protocol.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}}
        )
    )
    assert initialized["result"]["serverInfo"]["name"] == "patchbay-hub"
    assert "manager of multiple PatchBay machines" in initialized["result"]["instructions"]

    tools = asyncio.run(protocol.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}))
    names = {tool["name"] for tool in tools["result"]["tools"]}
    assert "patchbay_fleet_status" in names
    assert "patchbay_machine_recommend" in names
    assert "patchbay_worker_start" in names
    assert "patchbay_worker_start_auto" in names
    assert "patchbay_worker_integrate" in names
    status_tool = next(tool for tool in tools["result"]["tools"] if tool["name"] == "patchbay_worker_status")
    assert status_tool["annotations"]["readOnlyHint"] is False


def test_hub_protocol_queues_worker_start_for_machine(tmp_path):
    runtime = HubRuntime(hub_config(tmp_path))
    code = runtime.create_enrollment_code(name="Laptop")["code"]
    token = runtime.enroll_machine(code=code, machine_id="laptop", display_name="Laptop")["node_token"]
    runtime.heartbeat(machine_id="laptop", token=token, worker_status={"worker_lines": []})
    protocol = HubProtocol(runtime)

    fleet = call(protocol, "patchbay_fleet_status")
    assert fleet["machines"][0]["machine_id"] == "laptop"

    queued = call(
        protocol,
        "patchbay_worker_start",
        {"machine_id": "laptop", "name": "Reader", "brief": "Read the docs.", "workspace_mode": "read_only"},
    )
    assert queued["accepted"] is True
    assert queued["state"] == "queued"
    assert "arguments" not in queued

    claimed = runtime.claim_next_command(machine_id="laptop", token=token)
    assert claimed["command"]["action"] == "codex_worker_start"
    assert claimed["command"]["arguments"]["brief"] == "Read the docs."


def test_hub_protocol_auto_start_requires_enabled_routing(tmp_path):
    runtime = HubRuntime(hub_config(tmp_path))
    protocol = HubProtocol(runtime)

    result = call(protocol, "patchbay_worker_start_auto", {"name": "Reader", "brief": "Read the docs."})

    assert result["accepted"] is False
    assert result["error"] == "Hub availability routing is disabled."
    assert result["routing"]["enabled"] is False


def test_hub_protocol_auto_start_queues_to_least_busy_machine(tmp_path):
    runtime = HubRuntime(hub_config(tmp_path, routing_enabled=True))
    alpha_code = runtime.create_enrollment_code(name="Alpha")["code"]
    alpha_token = runtime.enroll_machine(code=alpha_code, machine_id="alpha", display_name="Alpha")["node_token"]
    beta_code = runtime.create_enrollment_code(name="Beta")["code"]
    beta_token = runtime.enroll_machine(code=beta_code, machine_id="beta", display_name="Beta")["node_token"]
    capabilities = {"codex_worker_tools": True, "max_concurrent_jobs": 4, "queue_enabled": True}
    runtime.heartbeat(
        machine_id="alpha",
        token=alpha_token,
        capabilities=capabilities,
        resource_status={
            "active_workers": 3,
            "max_concurrent_jobs": 4,
            "free_worker_slots": 1,
            "queue_enabled": True,
            "cpu_percent": 10,
            "memory_used_percent": 10,
            "memory_available_bytes": 8_000_000_000,
            "disk_free_bytes": 10_000_000_000,
            "disk_used_percent": 20,
        },
    )
    runtime.heartbeat(
        machine_id="beta",
        token=beta_token,
        capabilities=capabilities,
        resource_status={
            "active_workers": 0,
            "max_concurrent_jobs": 4,
            "free_worker_slots": 4,
            "queue_enabled": True,
            "cpu_percent": 40,
            "memory_used_percent": 40,
            "memory_available_bytes": 4_000_000_000,
            "disk_free_bytes": 10_000_000_000,
            "disk_used_percent": 20,
        },
    )
    protocol = HubProtocol(runtime)

    queued = call(
        protocol,
        "patchbay_worker_start_auto",
        {"name": "Auto Reader", "brief": "Read the docs.", "workspace_mode": "read_only"},
    )

    assert queued["accepted"] is True
    assert queued["machine_id"] == "beta"
    assert queued["routing"]["selected_machine_id"] == "beta"
    claimed = runtime.claim_next_command(machine_id="beta", token=beta_token)
    assert claimed["command"]["action"] == "codex_worker_start"
    assert claimed["command"]["arguments"]["name"] == "Auto Reader"


def test_hub_protocol_explicit_worker_start_unchanged_with_routing_enabled(tmp_path):
    runtime = HubRuntime(hub_config(tmp_path, routing_enabled=True))
    code = runtime.create_enrollment_code(name="Manual")["code"]
    token = runtime.enroll_machine(code=code, machine_id="manual", display_name="Manual")["node_token"]
    runtime.heartbeat(
        machine_id="manual",
        token=token,
        capabilities={"codex_worker_tools": True, "max_concurrent_jobs": 4, "queue_enabled": True},
        resource_status={
            "active_workers": 3,
            "max_concurrent_jobs": 4,
            "free_worker_slots": 1,
            "queue_enabled": True,
            "disk_free_bytes": 10_000_000_000,
        },
    )
    protocol = HubProtocol(runtime)

    queued = call(protocol, "patchbay_worker_start", {"machine_id": "manual", "name": "Manual", "brief": "Run there."})

    assert queued["accepted"] is True
    assert queued["machine_id"] == "manual"
    assert "routing" not in queued
