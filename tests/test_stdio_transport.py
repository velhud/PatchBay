import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from patchbay.protocol.resources import TOOL_CARD_URI


def subprocess_env(extra=None):
    env = dict(os.environ)
    entries = [entry for entry in env.get("PYTHONPATH", "").split(os.pathsep) if entry]
    if "src" not in entries:
        entries.insert(0, "src")
    env["PYTHONPATH"] = os.pathsep.join(entries)
    if extra:
        env.update(extra)
    return env


def base_config(root):
    return {
        "server": {
            "host": "127.0.0.1",
            "port": 8000,
            "max_concurrent_jobs": 1,
            "job_timeout_seconds": 60,
            "job_cleanup_after_hours": 1,
            "max_request_bytes": 1048576,
            "enable_cors": False,
        },
        "app": {"tool_mode": "worker", "widget_domain": "https://web-sandbox.oaiusercontent.com"},
        "auth": {
            "enabled": False,
            "token_env": "PATCHBAY_HTTP_TOKEN",
            "allow_query_token": True,
            "query_token_names": ["patchbay_token"],
            "require_for_non_loopback": True,
            "require_for_tunnel": True,
            "tunnel_mode": "none",
        },
        "repositories": {"default": str(root), "allowed": [str(root)]},
        "security": {"default_sandbox": "read-only"},
        "power_tools": {"direct_write": False, "bash_mode": "off", "codex_session_read": False},
        "logging": {
            "audit_file": str(root / "logs" / "audit.log"),
            "job_logs_dir": str(root / "logs" / "jobs"),
            "job_state_dir": str(root / "logs" / "jobs" / "state"),
            "access_log": False,
        },
    }


def test_stdio_transport_handles_core_mcp_methods(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(base_config(root)), encoding="utf-8")
    env = subprocess_env({"PATCHBAY_HOME": str(tmp_path / "home")})

    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "codex_tool_mode_info", "arguments": {}},
        },
        {"jsonrpc": "2.0", "id": 4, "method": "resources/list", "params": {}},
    ]

    completed = subprocess.run(
        [sys.executable, "-m", "patchbay.stdio", "--config", str(config_path), "--client-label", "unit-stdio"],
        cwd=".",
        env=env,
        input="\n".join(json.dumps(message) for message in messages) + "\n",
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0
    responses = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    by_id = {response["id"]: response for response in responses}
    assert set(by_id) == {1, 2, 3, 4}
    assert by_id[1]["result"]["serverInfo"]["name"] == "patchbay"
    tool_names = {tool["name"] for tool in by_id[2]["result"]["tools"]}
    assert "codex_tool_mode_info" in tool_names
    assert "codex_worker_start" in tool_names
    assert "codex_resume" not in tool_names
    assert by_id[3]["result"]["structuredContent"]["current_mode"] == "worker"
    assert by_id[4]["result"]["resources"] == []
    assert completed.stdout.count("Handling MCP method") == 0


def test_stdio_transport_exposes_resources_when_tool_cards_enabled(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    config = base_config(root)
    config["app"]["tool_cards"] = True
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    env = subprocess_env({"PATCHBAY_HOME": str(tmp_path / "home")})

    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}},
    ]

    completed = subprocess.run(
        [sys.executable, "-m", "patchbay.stdio", "--config", str(config_path), "--client-label", "unit-stdio"],
        cwd=".",
        env=env,
        input="\n".join(json.dumps(message) for message in messages) + "\n",
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0
    responses = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    by_id = {response["id"]: response for response in responses}
    tools = by_id[2]["result"]["tools"]
    assert any(tool.get("_meta", {}).get("openai/outputTemplate") == TOOL_CARD_URI for tool in tools)
    resource_uris = {resource["uri"] for resource in by_id[3]["result"]["resources"]}
    assert TOOL_CARD_URI in resource_uris
