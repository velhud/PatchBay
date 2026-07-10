from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from patchbay.hub import edge


def _runner(monkeypatch, *, config: dict[str, Any] | None = None, generation: str = "edgegen_test"):
    monkeypatch.setattr(edge, "JobManager", lambda config: SimpleNamespace())
    monkeypatch.setattr(edge, "JobExecutor", lambda config, manager: SimpleNamespace())
    monkeypatch.setattr(edge, "ToolHandler", lambda config, manager, executor: SimpleNamespace())
    return edge.EdgeRunner(
        config or {},
        profile={
            "hub_url": "https://hub.example",
            "machine_id": "edge-test",
            "node_token": "token",
            "edge_generation": generation,
        },
    )


def test_build_capabilities_advertises_v2_contract_and_edge_actions():
    try:
        tool_surface = edge.importlib.import_module("patchbay.hub.tool_surface")
    except Exception:
        pytest.skip("Hub V2 tool surface is landing concurrently")
    capabilities = edge.build_capabilities({"server": {"max_concurrent_jobs": 3}})

    assert capabilities["protocol_version"] == edge.EDGE_PROTOCOL_VERSION
    assert capabilities["contract_version"] == tool_surface.HUB_V2_CONTRACT_VERSION
    assert capabilities["manifest_hash"] == tool_surface.HUB_V2_MANIFEST_HASH
    assert capabilities["schema_hash"] == tool_surface.HUB_V2_SCHEMA_HASH
    assert capabilities["contract_hash"] == tool_surface.HUB_V2_CONTRACT_HASH
    assert capabilities["action_capability_version"] == tool_surface.HUB_V2_ACTION_CAPABILITY_VERSION
    assert capabilities["action_capabilities"] == capabilities["action_capability_versions"]
    assert capabilities["action_capabilities"]
    expected_actions = set(tool_surface.HUB_V2_EDGE_ACTION_MAP.values())
    expected_actions.update(tool_surface.HUB_V2_WORKSPACE_CHANGES_ACTION_MAP.values())
    assert set(capabilities["action_capabilities"]) == expected_actions


def test_build_capabilities_degrades_when_tool_surface_is_unavailable(monkeypatch):
    real_import = edge.importlib.import_module

    def import_module(name: str):
        if name == "patchbay.hub.tool_surface":
            raise ImportError("concurrent WP-00")
        return real_import(name)

    monkeypatch.setattr(edge.importlib, "import_module", import_module)

    capabilities = edge.build_capabilities({})

    assert capabilities["codex_worker_tools"] is True
    assert capabilities["protocol_version"] == edge.EDGE_PROTOCOL_VERSION
    assert capabilities["manifest_hash"] == ""
    assert capabilities["schema_hash"] == ""
    assert capabilities["contract_hash"] == ""
    assert capabilities["action_capabilities"] == {}


def test_enrollment_creates_one_immutable_generation_when_hub_omits_it(monkeypatch):
    saved: list[dict[str, Any]] = []
    monkeypatch.setattr(edge, "post_json", lambda *args, **kwargs: {"node_token": "node-token", "machine": {}})
    monkeypatch.setattr(
        edge,
        "save_edge_profile",
        lambda profile, environ=None: saved.append(dict(profile)) or "/tmp/edge-profile.json",
    )

    result = edge.enroll_edge({}, hub_url="https://hub.example", code="PB-TEST", machine_id="edge-test")

    generation = result["profile"]["edge_generation"]
    assert generation.startswith("edgegen_")
    assert saved[0]["edge_generation"] == generation
    assert edge._ensure_edge_generation(saved[0]) is False
    assert saved[0]["edge_generation"] == generation


@pytest.mark.asyncio
async def test_transport_payloads_include_generation_and_projection_revision(monkeypatch):
    runner = _runner(monkeypatch)
    posted: list[tuple[str, dict[str, Any]]] = []

    def post_json(hub_url, path, payload, **kwargs):
        posted.append((path, dict(payload)))
        return {"ok": True}

    async def status(handler):
        return {"active": 0}

    monkeypatch.setattr(edge, "post_json", post_json)
    monkeypatch.setattr(edge, "worker_status", status)
    monkeypatch.setattr(edge, "build_resource_status", lambda config, status: {"active_workers": 0})
    runner.profile["edge_generation"] = "edgegen_mutated"

    await runner.heartbeat()
    await runner.poll()
    await runner.send_result("cmd-1", result={"ok": True})
    await runner.heartbeat()

    assert [path for path, _ in posted] == ["/edge/heartbeat", "/edge/poll", "/edge/result", "/edge/heartbeat"]
    assert [payload["edge_generation"] for _, payload in posted] == ["edgegen_test"] * 4
    assert [payload["projection_revision"] for _, payload in posted] == [1, 1, 1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requirement",
    [
        {"required_protocol_version": "999"},
        {"required_contract_hash": "wrong-contract"},
        {"required_manifest_hash": "wrong-manifest"},
        {"required_schema_hash": "wrong-schema"},
        {"required_edge_generation": "edgegen_other"},
    ],
)
async def test_incompatible_claim_is_rejected_before_handler_execution(monkeypatch, requirement):
    runner = _runner(monkeypatch)
    handler_calls: list[str] = []
    sent: list[dict[str, Any]] = []

    async def handle_tool_call(action, arguments, context):
        handler_calls.append(action)
        return {"ok": True}

    async def send_result(command_id, *, result=None, error=""):
        payload = {"command_id": command_id, "result": result or {}, "error": error}
        sent.append(payload)
        return payload

    runner.handler = SimpleNamespace(handle_tool_call=handle_tool_call)
    monkeypatch.setattr(runner, "send_result", send_result)

    response = await runner.execute_command(
        {"command_id": "cmd-fenced", "action": "codex_worker_status", "arguments": {}, **requirement}
    )

    assert handler_calls == []
    assert response["result"]["reason"] == "incompatible_edge_contract"
    assert response["error"].startswith("incompatible_edge_contract:")
    assert sent == [response]


@pytest.mark.asyncio
async def test_matching_nested_requirements_allow_execution(monkeypatch):
    runner = _runner(monkeypatch)
    capabilities = edge.build_capabilities({})
    if not capabilities["action_capabilities"]:
        pytest.skip("Hub V2 tool surface is landing concurrently")
    action = next(iter(capabilities["action_capabilities"]))
    handler_calls: list[str] = []

    async def handle_tool_call(called_action, arguments, context):
        handler_calls.append(called_action)
        return {"ok": True}

    async def send_result(command_id, *, result=None, error=""):
        return {"command_id": command_id, "result": result or {}, "error": error}

    runner.handler = SimpleNamespace(handle_tool_call=handle_tool_call)
    monkeypatch.setattr(runner, "send_result", send_result)

    response = await runner.execute_command(
        {
            "command_id": "cmd-compatible",
            "action": action,
            "arguments": {},
            "requirements": {
                "protocol_version": capabilities["protocol_version"],
                "contract_hash": capabilities["contract_hash"],
                "edge_generation": runner.edge_generation,
                "action_capabilities": {action: capabilities["action_capabilities"][action]},
            },
        }
    )

    assert handler_calls == [action]
    assert response["error"] == ""
    assert response["result"] == {"ok": True}


@pytest.mark.asyncio
async def test_long_command_does_not_block_heartbeat_or_poll_and_shutdown_collects_tasks(monkeypatch):
    runner = _runner(monkeypatch, config={"hub": {"edge": {"max_concurrent_commands": 2}}})
    command_started = asyncio.Event()
    release_command = asyncio.Event()
    command_finished = asyncio.Event()
    heartbeat_count = 0
    poll_count = 0

    async def heartbeat():
        nonlocal heartbeat_count
        heartbeat_count += 1
        return {"heartbeat": heartbeat_count}

    async def poll():
        nonlocal poll_count
        poll_count += 1
        if poll_count == 1:
            return {"command": {"command_id": "cmd-long", "action": "codex_worker_wait", "arguments": {}}}
        return {"command": None}

    async def execute_command(command):
        command_started.set()
        await release_command.wait()
        command_finished.set()
        return {"ok": True}

    monkeypatch.setattr(runner, "heartbeat", heartbeat)
    monkeypatch.setattr(runner, "poll", poll)
    monkeypatch.setattr(runner, "execute_command", execute_command)

    loop_task = asyncio.create_task(runner.run_loop(interval_seconds=0.01))
    await asyncio.wait_for(command_started.wait(), timeout=1)
    await asyncio.sleep(0.06)

    assert heartbeat_count >= 3
    assert poll_count >= 3
    assert not command_finished.is_set()

    release_command.set()
    await asyncio.wait_for(command_finished.wait(), timeout=1)
    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task

    assert runner._command_tasks == set()
    assert runner.background_errors == ()


@pytest.mark.asyncio
async def test_background_command_execution_is_bounded(monkeypatch):
    runner = _runner(monkeypatch, config={"hub": {"edge": {"max_concurrent_commands": 2}}})
    release_commands = asyncio.Event()
    two_started = asyncio.Event()
    capacity_rejected = asyncio.Event()
    active = 0
    maximum_active = 0
    poll_count = 0
    sent: list[dict[str, Any]] = []

    async def heartbeat():
        return {"ok": True}

    async def poll():
        nonlocal poll_count
        poll_count += 1
        if poll_count <= 3:
            return {
                "command": {
                    "command_id": f"cmd-{poll_count}",
                    "action": "codex_worker_status",
                    "arguments": {},
                }
            }
        return {"command": None}

    async def execute_command(command):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        if active == 2:
            two_started.set()
        try:
            await release_commands.wait()
            return {"ok": True}
        finally:
            active -= 1

    async def send_result(command_id, *, result=None, error=""):
        payload = {"command_id": command_id, "result": result or {}, "error": error}
        sent.append(payload)
        capacity_rejected.set()
        return payload

    monkeypatch.setattr(runner, "heartbeat", heartbeat)
    monkeypatch.setattr(runner, "poll", poll)
    monkeypatch.setattr(runner, "execute_command", execute_command)
    monkeypatch.setattr(runner, "send_result", send_result)

    loop_task = asyncio.create_task(runner.run_loop(interval_seconds=0.01))
    await asyncio.wait_for(two_started.wait(), timeout=1)
    await asyncio.wait_for(capacity_rejected.wait(), timeout=1)

    assert maximum_active == 2
    assert sent[0]["command_id"] == "cmd-3"
    assert sent[0]["result"]["reason"] == "edge_execution_capacity"

    release_commands.set()
    await asyncio.sleep(0)
    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task

    assert runner._command_tasks == set()


@pytest.mark.asyncio
async def test_run_once_keeps_synchronous_result_contract(monkeypatch):
    runner = _runner(monkeypatch)
    monkeypatch.setattr(runner, "heartbeat", lambda: asyncio.sleep(0, result={"heartbeat": True}))
    monkeypatch.setattr(
        runner,
        "poll",
        lambda: asyncio.sleep(
            0,
            result={"command": {"command_id": "cmd-once", "action": "codex_worker_status", "arguments": {}}},
        ),
    )
    monkeypatch.setattr(runner, "execute_command", lambda command: asyncio.sleep(0, result={"sent": True}))

    result = await runner.run_once()

    assert result == {
        "heartbeat": {"heartbeat": True},
        "poll": {"command": {"command_id": "cmd-once", "action": "codex_worker_status", "arguments": {}}},
        "executed": True,
        "result": {"sent": True},
    }
