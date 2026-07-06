import asyncio
from pathlib import Path

from patchbay.hub.protocol import HubProtocol
from patchbay.hub.runtime import HubRuntime


def hub_config(tmp_path: Path):
    return {
        "hub": {"state_file": str(tmp_path / "hub-state.json")},
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
    assert "patchbay_worker_start" in names
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
