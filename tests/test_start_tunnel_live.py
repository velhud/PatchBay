import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


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
            "token_env": "PATCHBAY_HTTP_TOKEN",
            "allow_query_token": True,
            "query_token_names": ["patchbay_token"],
            "require_for_non_loopback": True,
            "require_for_tunnel": True,
            "tunnel_mode": "none",
        },
        "tunnel": {
            "cloudflared": "cloudflared",
            "ngrok": "ngrok",
            "cloudflare_token_env": "CLOUDFLARE_TUNNEL_TOKEN",
            "timeout_seconds": 45,
        },
        "repositories": {"default": str(root), "allowed": [str(root)]},
        "security": {
            "require_git_repo": False,
            "default_sandbox": "read-only",
            "allow_dangerously_bypass": False,
            "allowed_env_keys": ["PATH", "HOME"],
            "allowed_config_override_prefixes": [],
            "blocked_globs": [".git", ".git/**", ".env", "**/.env"],
        },
        "power_tools": {
            "direct_write": False,
            "bash_mode": "off",
            "bash_transcript": "compact",
            "bash_session_id": "",
            "require_bash_session": False,
            "codex_session_read": False,
        },
        "logging": {
            "audit_file": str(root / "logs" / "audit.log"),
            "job_logs_dir": str(root / "logs" / "jobs"),
            "job_state_dir": str(root / "logs" / "jobs" / "state"),
            "access_log": False,
        },
    }


def tunnel_ready_event(output):
    decoder = json.JSONDecoder()
    for index, char in enumerate(output):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("event") == "tunnel_ready":
            return payload
    raise AssertionError(output)


def test_start_script_supervises_fake_cloudflare_tunnel(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    fake_cloudflared = tmp_path / "fake-cloudflared"
    fake_cloudflared.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "if '--version' in sys.argv:\n"
        "    print('cloudflared fixture 0.0.0')\n"
        "    raise SystemExit(0)\n"
        "print('https://unit-test.trycloudflare.com', flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    fake_cloudflared.chmod(0o700)

    port = free_port()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(base_config(root)), encoding="utf-8")
    token_value = "fixture-" + "http-token"
    env = dict(os.environ)
    env["PATCHBAY_HOME"] = str(tmp_path / "home")
    env["PATCHBAY_HTTP_TOKEN"] = token_value

    process = subprocess.Popen(
        [
            sys.executable,
            "scripts/start.py",
            "--config",
            str(config_path),
            "--root",
            str(root),
            "--port",
            str(port),
            "--tunnel-mode",
            "cloudflare",
            "--cloudflared",
            str(fake_cloudflared),
            "--tunnel-timeout-seconds",
            "10",
            "--no-profile",
        ],
        cwd=".",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    output = ""
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            assert process.stdout is not None
            line = process.stdout.readline()
            if line:
                output += line
                if (
                    '"event": "tunnel_ready"' in output
                    and '"server_url"' in output
                    and '"runtime_status_path"' in output
                    and line.strip() == "}"
                ):
                    break
            elif process.poll() is not None:
                raise AssertionError(f"launcher exited early:\n{output}")
        assert '"event": "tunnel_ready"' in output
        token_query = "patchbay_" + "token"
        ready = tunnel_ready_event(output)
        parsed_url = urlparse(ready["server_url"])
        assert parsed_url.scheme == "https"
        assert parsed_url.hostname == "unit-test.trycloudflare.com"
        assert parsed_url.path == "/mcp"
        assert parse_qs(parsed_url.query) == {token_query: ["<redacted>"]}

        request = urllib.request.Request(f"http://127.0.0.1:{port}/")
        request.add_header("Authorization", f"Bearer {token_value}")
        with urllib.request.urlopen(request, timeout=3) as response:
            assert response.status == 200
            payload = json.loads(response.read().decode("utf-8"))
            assert payload["transport"] == "streamable-http"

        runtime_status = Path(ready["runtime_status_path"])
        assert runtime_status.exists()
        status = json.loads(runtime_status.read_text(encoding="utf-8"))
        status_text = json.dumps(status)
        assert token_value not in status_text
        status_url = urlparse(status["server_url"])
        assert status_url.hostname == "unit-test.trycloudflare.com"
        assert status_url.path == "/mcp"
        assert parse_qs(status_url.query) == {token_query: ["<redacted>"]}
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
