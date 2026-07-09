#!/usr/bin/env python3
"""Live HTTP smoke test for optional PatchBay Hub/Edge mode."""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from patchbay.hub.runtime import HubRuntime


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def post_json(url: str, payload: dict[str, Any], *, token: str = "", session_id: str = "") -> tuple[dict[str, Any], dict[str, str]]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8") or "{}"), dict(response.headers.items())


def header_value(headers: dict[str, str], name: str) -> str:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return ""


def wait_ready(base_url: str, token: str, timeout_seconds: float = 10) -> None:
    deadline = time.time() + timeout_seconds
    last_error = None
    while time.time() < deadline:
        try:
            request = urllib.request.Request(f"{base_url}/status", headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(request, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception as error:  # noqa: BLE001 - smoke script reports last connection failure.
            last_error = error
        time.sleep(0.2)
    raise RuntimeError(f"Hub did not become ready: {last_error}")


def config_payload(root: Path, port: int) -> dict[str, Any]:
    return {
        "server": {
            "host": "127.0.0.1",
            "port": port,
            "max_concurrent_jobs": 2,
            "queue_enabled": True,
            "job_timeout_seconds": 0,
            "job_cleanup_after_hours": 1,
            "max_request_bytes": 1_048_576,
            "enable_cors": False,
        },
        "app": {"tool_mode": "worker", "tool_cards": False},
        "auth": {
            "enabled": True,
            "token_env": "PATCHBAY_HTTP_TOKEN",
            "allow_query_token": True,
            "query_token_names": ["patchbay_token"],
            "require_for_non_loopback": True,
            "require_for_tunnel": True,
            "tunnel_mode": "none",
        },
        "hub": {
            "state_file": str(root / "hub-state.json"),
            "heartbeat_stale_seconds": 90,
            "routing": {
                "enabled": True,
                "min_disk_free_bytes": 1024,
                "allow_queue_when_full": False,
                "weights": {"worker_ratio": 0.60, "memory_ratio": 0.20, "cpu_ratio": 0.20},
            },
        },
        "repositories": {"default": str(root), "allowed": [str(root)]},
        "security": {"require_git_repo": False, "default_sandbox": "danger-full-access"},
        "power_tools": {"direct_write": True, "bash_mode": "full", "codex_session_read": True},
        "workers": {"status_recommended_poll_seconds": 30, "status_minimum_poll_seconds": 20},
        "logging": {
            "audit_file": str(root / "logs" / "audit.log"),
            "job_logs_dir": str(root / "logs" / "jobs"),
            "job_state_dir": str(root / "logs" / "jobs" / "state"),
            "access_log": False,
        },
    }


def run() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="patchbay-hub-live-") as tmp:
        root = Path(tmp)
        port = free_port()
        config = config_payload(root, port)
        config_path = root / "config.yaml"
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        token = "live-hub-token"
        env = dict(os.environ)
        env["PATCHBAY_CONFIG"] = str(config_path)
        env["PATCHBAY_HTTP_TOKEN"] = token
        env["PATCHBAY_HOME"] = str(root / "home")
        entries = [entry for entry in env.get("PYTHONPATH", "").split(os.pathsep) if entry]
        source = str(SRC_ROOT)
        if source not in entries:
            entries.insert(0, source)
        env["PYTHONPATH"] = os.pathsep.join(entries)

        code_runtime = HubRuntime(config)
        edge_codes = {
            "live-edge": code_runtime.create_enrollment_code(name="Live Edge", tags=["live", "idle"])["code"],
            "busy-edge": code_runtime.create_enrollment_code(name="Busy Edge", tags=["live", "busy"])["code"],
            "hot-edge": code_runtime.create_enrollment_code(name="Hot Edge", tags=["live", "hot"])["code"],
        }
        process = subprocess.Popen(
            [sys.executable, "-m", "patchbay.hub.server"],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        base_url = f"http://127.0.0.1:{port}"
        try:
            wait_ready(base_url, token)

            enrollments: dict[str, dict[str, Any]] = {}
            node_tokens: dict[str, str] = {}
            for machine_id, code in edge_codes.items():
                enroll, _ = post_json(
                    f"{base_url}/edge/enroll",
                    {
                        "code": code,
                        "machine_id": machine_id,
                        "display_name": machine_id.replace("-", " ").title(),
                        "tags": ["live"],
                        "capabilities": {"codex_worker_tools": True, "max_concurrent_jobs": 4, "queue_enabled": True},
                        "workspaces": [
                            {"alias": "tmp", "path": str(root), "git": True},
                            {"alias": "repos", "path": str(root / "repos"), "exists": True, "git": False},
                        ],
                    },
                )
                enrollments[machine_id] = enroll
                node_tokens[machine_id] = enroll["node_token"]
            heartbeat, _ = post_json(
                f"{base_url}/edge/heartbeat",
                {
                    "machine_id": "live-edge",
                    "worker_status": {"worker_lines": ["live worker: idle"]},
                    "resource_status": {
                        "active_workers": 0,
                        "max_concurrent_jobs": 4,
                        "free_worker_slots": 4,
                        "queue_enabled": True,
                        "cpu_percent": 5,
                        "memory_used_percent": 10,
                        "memory_available_bytes": 9_000_000_000,
                        "disk_free_bytes": 10_000_000,
                        "disk_used_percent": 20,
                    },
                },
                token=node_tokens["live-edge"],
            )
            post_json(
                f"{base_url}/edge/heartbeat",
                {
                    "machine_id": "busy-edge",
                    "worker_status": {"worker_lines": ["worker A: running", "worker B: running"]},
                    "resource_status": {
                        "active_workers": 2,
                        "max_concurrent_jobs": 4,
                        "free_worker_slots": 2,
                        "queue_enabled": True,
                        "cpu_percent": 30,
                        "memory_used_percent": 40,
                        "memory_available_bytes": 6_000_000_000,
                        "disk_free_bytes": 10_000_000,
                        "disk_used_percent": 25,
                    },
                },
                token=node_tokens["busy-edge"],
            )
            post_json(
                f"{base_url}/edge/heartbeat",
                {
                    "machine_id": "hot-edge",
                    "worker_status": {"worker_lines": ["worker A: running"]},
                    "resource_status": {
                        "active_workers": 1,
                        "max_concurrent_jobs": 4,
                        "free_worker_slots": 3,
                        "queue_enabled": True,
                        "cpu_percent": 95,
                        "memory_used_percent": 90,
                        "memory_available_bytes": 1_000_000_000,
                        "disk_free_bytes": 10_000_000,
                        "disk_used_percent": 75,
                    },
                },
                token=node_tokens["hot-edge"],
            )
            init_response, init_headers = post_json(
                f"{base_url}/mcp",
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
                token=token,
            )
            session_id = header_value(init_headers, "Mcp-Session-Id")
            if not session_id:
                raise RuntimeError("Hub MCP initialize did not return Mcp-Session-Id")
            fleet, _ = post_json(
                f"{base_url}/mcp",
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "patchbay_fleet_status", "arguments": {}}},
                token=token,
                session_id=session_id,
            )
            queued, _ = post_json(
                f"{base_url}/mcp",
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "patchbay_worker_start",
                        "arguments": {
                            "machine_id": "live-edge",
                            "name": "Smoke",
                            "brief": "No-op smoke task.",
                            "ungrouped_reason": "tiny_check",
                        },
                    },
                },
                token=token,
                session_id=session_id,
            )
            command_id = queued["result"]["structuredContent"]["command_id"]
            polled, _ = post_json(f"{base_url}/edge/poll", {"machine_id": "live-edge"}, token=node_tokens["live-edge"])
            if polled["command"]["command_id"] != command_id:
                raise RuntimeError("Edge did not claim the queued command")
            done, _ = post_json(
                f"{base_url}/edge/result",
                {"machine_id": "live-edge", "command_id": command_id, "result": {"ok": True}},
                token=node_tokens["live-edge"],
            )
            recommendation, _ = post_json(
                f"{base_url}/mcp",
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {"name": "patchbay_machine_recommend", "arguments": {}},
                },
                token=token,
                session_id=session_id,
            )
            selected = recommendation["result"]["structuredContent"]["selected_machine_id"]
            if selected != "live-edge":
                raise RuntimeError(f"Router selected {selected}, expected live-edge")
            group_created, _ = post_json(
                f"{base_url}/mcp",
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {
                        "name": "patchbay_work_group_create",
                        "arguments": {
                            "title": "Live grouped smoke",
                            "goal": "Verify Hub group pinning and preflight.",
                            "repo_path": str(root),
                            "lanes": ["smoke", "followup"],
                        },
                    },
                },
                token=token,
                session_id=session_id,
            )
            group_payload = group_created["result"]["structuredContent"]["work_group"]
            work_group_id = group_payload["work_group_id"]
            if group_payload["pinned_machine_id"] != "live-edge":
                raise RuntimeError(f"Group pinned {group_payload['pinned_machine_id']}, expected live-edge")
            preflight_polled, _ = post_json(f"{base_url}/edge/poll", {"machine_id": "live-edge"}, token=node_tokens["live-edge"])
            if preflight_polled["command"]["action"] != "patchbay_edge_preflight":
                raise RuntimeError("Edge did not claim group preflight command")
            post_json(
                f"{base_url}/edge/result",
                {
                    "machine_id": "live-edge",
                    "command_id": preflight_polled["command"]["command_id"],
                    "result": {"ok": True, "repo_exists": True, "git_repo": False, "branch": "", "head": ""},
                },
                token=node_tokens["live-edge"],
            )
            auto_queued, _ = post_json(
                f"{base_url}/mcp",
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {
                        "name": "patchbay_worker_start_auto",
                        "arguments": {
                            "work_group_id": work_group_id,
                            "lane": "smoke",
                            "auto_routing_ok": True,
                            "name": "Auto Smoke",
                            "brief": "No-op auto smoke task.",
                        },
                    },
                },
                token=token,
                session_id=session_id,
            )
            auto_payload = auto_queued["result"]["structuredContent"]
            if auto_payload["machine_id"] != "live-edge":
                raise RuntimeError(f"Auto worker queued on {auto_payload['machine_id']}, expected live-edge")
            auto_polled, _ = post_json(f"{base_url}/edge/poll", {"machine_id": "live-edge"}, token=node_tokens["live-edge"])
            if auto_polled["command"]["command_id"] != auto_payload["command_id"]:
                raise RuntimeError("Edge did not claim the auto-routed command")
            post_json(
                f"{base_url}/edge/result",
                {"machine_id": "live-edge", "command_id": auto_payload["command_id"], "result": {"ok": True}},
                token=node_tokens["live-edge"],
            )
            post_json(
                f"{base_url}/edge/heartbeat",
                {
                    "machine_id": "live-edge",
                    "worker_status": {"worker_lines": ["worker A: running", "worker B: running", "worker C: running"]},
                    "resource_status": {
                        "active_workers": 3,
                        "max_concurrent_jobs": 4,
                        "free_worker_slots": 1,
                        "queue_enabled": True,
                        "cpu_percent": 90,
                        "memory_used_percent": 90,
                        "memory_available_bytes": 1_000_000_000,
                        "disk_free_bytes": 10_000_000,
                        "disk_used_percent": 30,
                    },
                },
                token=node_tokens["live-edge"],
            )
            post_json(
                f"{base_url}/edge/heartbeat",
                {
                    "machine_id": "busy-edge",
                    "worker_status": {"worker_lines": []},
                    "resource_status": {
                        "active_workers": 0,
                        "max_concurrent_jobs": 4,
                        "free_worker_slots": 4,
                        "queue_enabled": True,
                        "cpu_percent": 15,
                        "memory_used_percent": 20,
                        "memory_available_bytes": 8_000_000_000,
                        "disk_free_bytes": 10_000_000,
                        "disk_used_percent": 25,
                    },
                },
                token=node_tokens["busy-edge"],
            )
            changed_recommendation, _ = post_json(
                f"{base_url}/mcp",
                {
                    "jsonrpc": "2.0",
                    "id": 8,
                    "method": "tools/call",
                    "params": {"name": "patchbay_machine_recommend", "arguments": {}},
                },
                token=token,
                session_id=session_id,
            )
            changed_selected = changed_recommendation["result"]["structuredContent"]["selected_machine_id"]
            if changed_selected != "busy-edge":
                raise RuntimeError(f"Router selected {changed_selected}, expected busy-edge after heartbeat load changed")
            group_recommendation, _ = post_json(
                f"{base_url}/mcp",
                {
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "tools/call",
                    "params": {"name": "patchbay_machine_recommend", "arguments": {"work_group_id": work_group_id}},
                },
                token=token,
                session_id=session_id,
            )
            group_selected = group_recommendation["result"]["structuredContent"]["selected_machine_id"]
            if group_selected != "live-edge":
                raise RuntimeError(f"Grouped recommendation selected {group_selected}, expected pinned live-edge")
            second_auto, _ = post_json(
                f"{base_url}/mcp",
                {
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/call",
                    "params": {
                        "name": "patchbay_worker_start_auto",
                        "arguments": {
                            "work_group_id": work_group_id,
                            "lane": "followup",
                            "auto_routing_ok": True,
                            "name": "Auto Followup",
                            "brief": "No-op grouped follow-up task.",
                        },
                    },
                },
                token=token,
                session_id=session_id,
            )
            second_payload = second_auto["result"]["structuredContent"]
            if second_payload["machine_id"] != "live-edge":
                raise RuntimeError(f"Second grouped auto worker queued on {second_payload['machine_id']}, expected live-edge")
            second_polled, _ = post_json(f"{base_url}/edge/poll", {"machine_id": "live-edge"}, token=node_tokens["live-edge"])
            if second_polled["command"]["command_id"] != second_payload["command_id"]:
                raise RuntimeError("Edge did not claim the second grouped command")
            post_json(
                f"{base_url}/edge/result",
                {"machine_id": "live-edge", "command_id": second_payload["command_id"], "result": {"ok": True}},
                token=node_tokens["live-edge"],
            )
            command_status, _ = post_json(
                f"{base_url}/mcp",
                {
                    "jsonrpc": "2.0",
                    "id": 11,
                    "method": "tools/call",
                    "params": {"name": "patchbay_command_status", "arguments": {"command_id": command_id}},
                },
                token=token,
                session_id=session_id,
            )
            state = command_status["result"]["structuredContent"]["commands"][0]["state"]
            if state != "completed":
                raise RuntimeError(f"Command state was {state}, expected completed")
            friendly_group, _ = post_json(
                f"{base_url}/mcp",
                {
                    "jsonrpc": "2.0",
                    "id": 12,
                    "method": "tools/call",
                    "params": {
                        "name": "patchbay_work_group_create",
                        "arguments": {
                            "title": "Friendly repo smoke",
                            "goal": "Verify a human repo name resolves under an advertised workspace root.",
                            "repo_path": "RetailMind",
                            "allowed_machine_ids": ["live-edge"],
                            "lanes": ["preflight"],
                        },
                    },
                },
                token=token,
                session_id=session_id,
            )
            friendly_payload = friendly_group["result"]["structuredContent"]["work_group"]
            expected_friendly_repo = str(root / "repos" / "RetailMind")
            if friendly_payload["requested_repo_path"] != "RetailMind":
                raise RuntimeError("Friendly group did not preserve requested repo_path")
            if friendly_payload["repo_path"] != expected_friendly_repo:
                raise RuntimeError(f"Friendly group resolved {friendly_payload['repo_path']}, expected {expected_friendly_repo}")
            friendly_preflight, _ = post_json(f"{base_url}/edge/poll", {"machine_id": "live-edge"}, token=node_tokens["live-edge"])
            if friendly_preflight["command"]["action"] != "patchbay_edge_preflight":
                raise RuntimeError("Edge did not claim friendly group preflight command")
            if friendly_preflight["command"]["arguments"]["repo_path"] != expected_friendly_repo:
                raise RuntimeError("Friendly preflight did not receive the resolved machine-local repo_path")
            post_json(
                f"{base_url}/edge/result",
                {
                    "machine_id": "live-edge",
                    "command_id": friendly_preflight["command"]["command_id"],
                    "result": {"ok": True, "repo_exists": True, "git_repo": True, "branch": "main", "head": "abc123"},
                },
                token=node_tokens["live-edge"],
            )
            return {
                "ok": True,
                "base_url": base_url,
                "machine_id": enrollments["live-edge"]["machine"]["machine_id"],
                "heartbeat_accepted": heartbeat["accepted"],
                "mcp_server": init_response["result"]["serverInfo"]["name"],
                "fleet_summary": fleet["result"]["structuredContent"]["summary"],
                "command_id": command_id,
                "auto_command_id": auto_payload["command_id"],
                "work_group_id": work_group_id,
                "second_auto_command_id": second_payload["command_id"],
                "initial_router_selection": selected,
                "changed_router_selection": changed_selected,
                "group_router_selection_after_load_change": group_selected,
                "completed_state": done["state"],
                "friendly_repo_requested": friendly_payload["requested_repo_path"],
                "friendly_repo_resolved": friendly_payload["repo_path"],
            }
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = run()
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
