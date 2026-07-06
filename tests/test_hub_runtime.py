from pathlib import Path

from patchbay.hub.runtime import HubRuntime
from patchbay.hub.store import HubStore


def hub_config(tmp_path: Path):
    return {
        "hub": {"state_file": str(tmp_path / "hub-state.json"), "heartbeat_stale_seconds": 90},
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
