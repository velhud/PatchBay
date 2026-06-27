import json
import os
import subprocess
import sys

from patchbay.connector.status import connector_status


def base_config(auth=None):
    return {
        "server": {"host": "127.0.0.1", "port": 8000, "enable_cors": False},
        "auth": {
            "enabled": False,
            "token_env": "PATCHBAY_HTTP_TOKEN",
            "allow_query_token": True,
            "query_token_names": ["patchbay_token", "token"],
            "require_for_non_loopback": True,
            "require_for_tunnel": True,
            "tunnel_mode": "none",
            **(auth or {}),
        },
        "repositories": {"default": ".", "allowed": ["."]},
        "security": {"default_sandbox": "read-only"},
        "logging": {"access_log": False},
    }


def test_connector_status_local_ready_without_token():
    status = connector_status(base_config(), environ={})

    assert status["ready"] is True
    assert status["connection"]["local_mcp_url"] == "http://127.0.0.1:8000/mcp"
    assert status["connection"]["server_url"] == "http://127.0.0.1:8000/mcp"
    assert status["auth"]["enabled"] is False
    assert status["power_tools"]["direct_write"] is False
    assert status["power_tools"]["bash_mode"] == "off"
    assert status["power_tools"]["codex_session_read"] is False


def test_connector_status_redacts_query_token_url():
    query_token_name = "patchbay_" + "token"
    status = connector_status(
        base_config(),
        environ={"PATCHBAY_HTTP_TOKEN": "auth-fixture-value"},
        public_base_url="https://bridge.example",
    )

    assert status["ready"] is True
    assert status["connection"]["server_url"] == f"https://bridge.example/mcp?{query_token_name}=%3Credacted%3E"
    assert status["connection"]["query_token_url_redacted"] is True
    assert "auth-fixture-value" not in json.dumps(status)


def test_connector_status_reveals_query_token_only_when_requested():
    query_token_name = "patchbay_" + "token"
    token_value = "auth-fixture-value"
    status = connector_status(
        base_config(),
        environ={"PATCHBAY_HTTP_TOKEN": token_value},
        public_base_url="https://bridge.example",
        reveal_token=True,
    )

    assert status["connection"]["server_url"] == f"https://bridge.example/mcp?{query_token_name}={token_value}"
    assert status["auth"]["token_returned"] is True


def test_connector_status_reports_fail_closed_tunnel_without_token():
    status = connector_status(base_config(auth={"tunnel_mode": "cloudflare"}), environ={})

    assert status["ready"] is False
    failed = {check["name"]: check for check in status["checks"] if check["status"] == "fail"}
    assert "http_auth" in failed


def test_doctor_script_json_output():
    env = dict(os.environ)
    for name in ["PATCHBAY_HTTP_TOKEN", "PATCHBAY_TOKEN"]:
        env.pop(name, None)
    completed = subprocess.run(
        [sys.executable, "scripts/doctor.py", "--json"],
        cwd=".",
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["name"] == "patchbay"
    assert payload["ready"] is True
    assert payload["connection"]["server_url"] == "http://127.0.0.1:8000/mcp"
