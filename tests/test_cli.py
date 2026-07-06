import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from patchbay.cli import settings_main


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
            "allowed_origins": ["http://127.0.0.1:3000"],
        },
        "app": {"widget_domain": "https://web-sandbox.oaiusercontent.com", "tool_mode": "worker"},
        "auth": {
            "enabled": False,
            "token_env": "PATCHBAY_HTTP_TOKEN",
            "allow_query_token": True,
            "query_token_names": ["patchbay_token"],
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


def test_patchbay_cli_help_lists_public_commands():
    completed = subprocess.run(
        [sys.executable, "-m", "patchbay.cli", "--help"],
        cwd=".",
        env=subprocess_env(),
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0
    assert "patchbay setup" in completed.stdout
    assert "patchbay hub start" in completed.stdout
    assert "patchbay edge enroll" in completed.stdout
    assert "patchbay stdio" in completed.stdout
    assert "patchbay install-cloudflared" in completed.stdout


def test_patchbay_cli_start_print_only_json(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(base_config(root)), encoding="utf-8")
    env = subprocess_env({"PATCHBAY_HOME": str(tmp_path / "home")})
    env.pop("PATCHBAY_HTTP_TOKEN", None)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "patchbay.cli",
            "start",
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
    assert payload["name"] == "patchbay"
    assert payload["setup_guide"]["tool_mode"] == "worker"
    assert payload["connection"]["local_mcp_url"].endswith("/mcp")


def test_patchbay_setup_fails_in_noninteractive_shell(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(base_config(root)), encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "-m", "patchbay.cli", "setup", "--config", str(config_path)],
        cwd=".",
        env=subprocess_env(),
        input="",
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 2
    assert "patchbay setup is interactive" in completed.stderr


def test_settings_set_list_show_delete_round_trip(tmp_path, monkeypatch, capsys):
    root = tmp_path / "repo"
    root.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(base_config(root)), encoding="utf-8")
    monkeypatch.setenv("PATCHBAY_HOME", str(tmp_path / "home"))

    assert settings_main(["set", "--config", str(config_path), "--root", str(root), "--tool-mode", "worker", "--port", "8123", "--json"]) == 0
    saved = json.loads(capsys.readouterr().out.split("\nProfile saved:", maxsplit=1)[0])
    assert saved["setup_guide"]["tool_mode"] == "worker"

    assert settings_main(["list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["profiles"][0]["root"] == str(root.resolve())

    assert settings_main(["show", "--root", str(root), "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["server"]["port"] == 8123

    assert settings_main(["delete", "--root", str(root)]) == 0
    assert "Deleted profile" in capsys.readouterr().out
