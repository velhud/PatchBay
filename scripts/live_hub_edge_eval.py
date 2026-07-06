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
        "hub": {"state_file": str(root / "hub-state.json"), "heartbeat_stale_seconds": 90},
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
        source = str(Path(__file__).resolve().parents[1] / "src")
        if source not in entries:
            entries.insert(0, source)
        env["PYTHONPATH"] = os.pathsep.join(entries)

        code = HubRuntime(config).create_enrollment_code(name="Live Edge", tags=["live"])["code"]
        process = subprocess.Popen(
            [sys.executable, "-m", "patchbay.hub.server"],
            cwd=str(Path(__file__).resolve().parents[1]),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        base_url = f"http://127.0.0.1:{port}"
        try:
            wait_ready(base_url, token)

            enroll, _ = post_json(
                f"{base_url}/edge/enroll",
                {
                    "code": code,
                    "machine_id": "live-edge",
                    "display_name": "Live Edge",
                    "tags": ["live"],
                    "capabilities": {"codex_worker_tools": True},
                    "workspaces": [{"alias": "tmp", "path": str(root)}],
                },
            )
            node_token = enroll["node_token"]
            heartbeat, _ = post_json(
                f"{base_url}/edge/heartbeat",
                {"machine_id": "live-edge", "worker_status": {"worker_lines": ["live worker: idle"]}},
                token=node_token,
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
                        "arguments": {"machine_id": "live-edge", "name": "Smoke", "brief": "No-op smoke task."},
                    },
                },
                token=token,
                session_id=session_id,
            )
            command_id = queued["result"]["structuredContent"]["command_id"]
            polled, _ = post_json(f"{base_url}/edge/poll", {"machine_id": "live-edge"}, token=node_token)
            if polled["command"]["command_id"] != command_id:
                raise RuntimeError("Edge did not claim the queued command")
            done, _ = post_json(
                f"{base_url}/edge/result",
                {"machine_id": "live-edge", "command_id": command_id, "result": {"ok": True}},
                token=node_token,
            )
            command_status, _ = post_json(
                f"{base_url}/mcp",
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "patchbay_command_status", "arguments": {"command_id": command_id}},
                },
                token=token,
                session_id=session_id,
            )
            state = command_status["result"]["structuredContent"]["commands"][0]["state"]
            if state != "completed":
                raise RuntimeError(f"Command state was {state}, expected completed")
            return {
                "ok": True,
                "base_url": base_url,
                "machine_id": enroll["machine"]["machine_id"],
                "heartbeat_accepted": heartbeat["accepted"],
                "mcp_server": init_response["result"]["serverInfo"]["name"],
                "fleet_summary": fleet["result"]["structuredContent"]["summary"],
                "command_id": command_id,
                "completed_state": done["state"],
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
