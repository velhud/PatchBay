import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from launcher import launcher_json_payload, prepare_start
from profile_store import read_workspace_profile


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
            "allowed_origins": ["http://127.0.0.1:3000"],
        },
        "app": {"widget_domain": "https://web-sandbox.oaiusercontent.com"},
        "auth": {
            "enabled": False,
            "token_env": "CODEX_MCP_HTTP_TOKEN",
            "allow_query_token": True,
            "query_token_names": ["codex_mcp_token"],
            "require_for_non_loopback": True,
            "require_for_tunnel": True,
            "tunnel_mode": "none",
        },
        "tunnel": {
            "hostname": "",
            "tunnel_name": "",
            "cloudflared": "cloudflared",
            "ngrok": "ngrok",
            "cloudflare_token_env": "CLOUDFLARE_TUNNEL_TOKEN",
            "timeout_seconds": 45,
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


def test_prepare_start_applies_cli_overrides_and_writes_runtime_config(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()
    env = {"CODEX_MCP_HOME": str(tmp_path / "home")}

    prepared = prepare_start(
        base_config(root),
        root=str(root),
        allow_roots=[str(extra)],
        port=8123,
        direct_write=True,
        bash_mode="safe",
        codex_session_read=True,
        use_profile=False,
        environ=env,
    )

    config = prepared["runtime_config"]
    assert config["repositories"]["default"] == str(root.resolve())
    assert config["repositories"]["allowed"] == [str(root.resolve()), str(extra.resolve())]
    assert config["server"]["port"] == 8123
    assert config["power_tools"]["direct_write"] is True
    assert config["power_tools"]["bash_mode"] == "safe"
    assert config["power_tools"]["codex_session_read"] is True
    assert Path(prepared["runtime_config_path"]).exists()
    assert prepared["status"]["ready"] is True


def test_prepare_start_accepts_worker_tool_mode(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    env = {"CODEX_MCP_HOME": str(tmp_path / "home")}

    prepared = prepare_start(
        base_config(root),
        root=str(root),
        tool_mode="worker",
        use_profile=False,
        environ=env,
    )

    assert prepared["runtime_config"]["app"]["tool_mode"] == "worker"
    assert prepared["status"]["ready"] is True


def test_prepare_start_public_url_requires_token(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    env = {"CODEX_MCP_HOME": str(tmp_path / "home")}

    prepared = prepare_start(
        base_config(root),
        root=str(root),
        public_base_url="https://bridge.example",
        use_profile=False,
        environ=env,
    )

    assert prepared["runtime_config"]["auth"]["tunnel_mode"] == "custom"
    assert prepared["status"]["ready"] is False
    assert any(check["name"] == "http_auth" and check["status"] == "fail" for check in prepared["status"]["checks"])


def test_prepare_start_process_tunnel_requires_token_and_records_tunnel_config(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    env = {"CODEX_MCP_HOME": str(tmp_path / "home")}

    prepared = prepare_start(
        base_config(root),
        root=str(root),
        tunnel_mode="cloudflare",
        cloudflared="/tmp/fake-cloudflared",
        tunnel_timeout_seconds=12,
        use_profile=False,
        environ=env,
    )

    assert prepared["runtime_config"]["auth"]["tunnel_mode"] == "cloudflare"
    assert prepared["runtime_config"]["tunnel"]["cloudflared"] == "/tmp/fake-cloudflared"
    assert prepared["runtime_config"]["tunnel"]["timeout_seconds"] == 12
    assert prepared["status"]["ready"] is False


def test_prepare_start_saves_tunnel_profile_without_token_values(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    token_value = "fixture-" + "token-value"
    env = {"CODEX_MCP_HOME": str(tmp_path / "home"), "CODEX_MCP_HTTP_TOKEN": token_value}

    saved = prepare_start(
        base_config(root),
        root=str(root),
        tunnel_mode="ngrok",
        hostname="codex.ngrok-free.app",
        ngrok="/tmp/fake-ngrok",
        save_profile=True,
        use_profile=False,
        environ=env,
    )

    raw_profile = Path(saved["profile"]["profile_path"]).read_text(encoding="utf-8")
    assert token_value not in raw_profile
    profile = read_workspace_profile(root, env)
    assert profile["auth"]["tunnel_mode"] == "ngrok"
    assert profile["tunnel"]["hostname"] == "codex.ngrok-free.app"
    assert profile["tunnel"]["ngrok"] == "/tmp/fake-ngrok"


def test_prepare_start_saves_and_reuses_profile_without_tokens(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    token_value = "fixture-" + "token-value"
    env = {"CODEX_MCP_HOME": str(tmp_path / "home"), "CODEX_MCP_HTTP_TOKEN": token_value}

    saved = prepare_start(
        base_config(root),
        root=str(root),
        public_base_url="https://bridge.example",
        bash_mode="safe",
        save_profile=True,
        use_profile=False,
        environ=env,
    )

    raw_profile = Path(saved["profile"]["profile_path"]).read_text(encoding="utf-8")
    assert token_value not in raw_profile
    assert read_workspace_profile(root, env)["power_tools"]["bash_mode"] == "safe"

    reused = prepare_start(base_config(root), root=str(root), use_profile=True, environ=env)
    assert reused["profile"]["used"] is True
    expected_query = "codex_" + "mcp_token=%3Credacted%3E"
    assert reused["status"]["connection"]["server_url"] == f"https://bridge.example/mcp?{expected_query}"
    assert reused["runtime_config"]["power_tools"]["bash_mode"] == "safe"


def test_launcher_json_payload_is_bounded(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    env = {"CODEX_MCP_HOME": str(tmp_path / "home")}
    prepared = prepare_start(base_config(root), root=str(root), use_profile=False, environ=env)

    payload = launcher_json_payload(prepared)

    assert payload["name"] == "codex-mcp-wrapper"
    assert payload["ready"] is True
    assert "runtime_config" not in payload
    assert payload["connection"]["local_mcp_url"].endswith("/mcp")


def test_start_script_print_only_json(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(base_config(root)), encoding="utf-8")
    env = dict(os.environ)
    env["CODEX_MCP_HOME"] = str(tmp_path / "home")
    env.pop("CODEX_MCP_HTTP_TOKEN", None)

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/start.py",
            "--config",
            str(config_path),
            "--root",
            str(root),
            "--port",
            "8124",
            "--print-only",
            "--json",
            "--no-profile",
        ],
        cwd=".",
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["ready"] is True
    assert payload["connection"]["local_mcp_url"] == "http://127.0.0.1:8124/mcp"
    assert Path(payload["runtime_config_path"]).exists()


def test_start_script_accepts_worker_tool_mode(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(base_config(root)), encoding="utf-8")
    env = dict(os.environ)
    env["CODEX_MCP_HOME"] = str(tmp_path / "home")
    env.pop("CODEX_MCP_HTTP_TOKEN", None)

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/start.py",
            "--config",
            str(config_path),
            "--root",
            str(root),
            "--tool-mode",
            "worker",
            "--print-only",
            "--json",
            "--no-profile",
        ],
        cwd=".",
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["ready"] is True
    runtime_config = yaml.safe_load(Path(payload["runtime_config_path"]).read_text(encoding="utf-8"))
    assert runtime_config["app"]["tool_mode"] == "worker"


def test_start_script_print_only_reveal_token_is_explicit(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(base_config(root)), encoding="utf-8")
    token_value = "fixture-token-value"
    env = dict(os.environ)
    env["CODEX_MCP_HOME"] = str(tmp_path / "home")
    env["CODEX_MCP_HTTP_TOKEN"] = token_value

    redacted = subprocess.run(
        [
            sys.executable,
            "scripts/start.py",
            "--config",
            str(config_path),
            "--root",
            str(root),
            "--public-base-url",
            "https://bridge.example",
            "--tool-mode",
            "worker",
            "--print-only",
            "--json",
            "--no-profile",
        ],
        cwd=".",
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    revealed = subprocess.run(
        [
            sys.executable,
            "scripts/start.py",
            "--config",
            str(config_path),
            "--root",
            str(root),
            "--public-base-url",
            "https://bridge.example",
            "--tool-mode",
            "worker",
            "--print-only",
            "--json",
            "--reveal-token",
            "--no-profile",
        ],
        cwd=".",
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert redacted.returncode == 0
    assert token_value not in redacted.stdout
    assert "%3Credacted%3E" in redacted.stdout

    assert revealed.returncode == 0
    assert token_value in revealed.stdout
    assert "WARNING: printing a private tokenized ChatGPT Server URL" in revealed.stderr
    payload = json.loads(revealed.stdout)
    assert payload["auth"]["token_returned"] is True
