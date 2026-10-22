"""S08 recording/file/HTTP fixtures; no live tunnel, deployment or model."""
import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from patchbay.auth import AuthPolicy, request_origin_allowed
from patchbay.connector import profiles


def test_immutable_config_generations_cannot_replace_an_outstanding_launch(tmp_path):
    env = {"PATCHBAY_HOME": str(tmp_path / "private")}
    first = profiles.write_runtime_config(tmp_path, {"port": 8000}, env, immutable=True)
    before = Path(first).read_bytes()
    second = profiles.write_runtime_config(tmp_path, {"port": 8001}, env, immutable=True)
    profiles.write_runtime_config(tmp_path, {"port": 8002}, env)  # mutable preferences/metadata path
    assert first != second and Path(first).read_bytes() == before
    assert Path(second).read_bytes() != before


def test_permission_failure_preserves_previous_private_config(tmp_path, monkeypatch):
    target = tmp_path / "config.yaml"; target.write_text("old\n")
    def fail(*args): raise OSError("controlled permission failure")
    monkeypatch.setattr(profiles.os, "fchmod", fail)
    with pytest.raises(OSError): profiles._write_private_text(target, "new\n")
    assert target.read_text() == "old\n" and not list(tmp_path.glob(".publish-*"))


@pytest.mark.parametrize("origin,expected", [
    (None, True), ("https://trusted.example", True), ("https://trusted.example:443", True), ("https://trusted.example:0", False), ("https://evil.example", False),
    ("null", False), ("", False), ("https://trusted.example/path", False),
    ("https://trusted.example@evil.example", False), ("https://trusted.example, https://evil.example", False),
])
def test_origin_is_explicit_independent_configuration(origin, expected):
    config = {"server": {"allowed_origins": ["https://trusted.example"]}}
    headers = {"Host": "trusted.example", "X-Forwarded-Host": "trusted.example"}
    if origin is not None: headers["Origin"] = origin
    assert request_origin_allowed(config, headers) is expected


def test_real_manager_http_refuses_origin_before_session_and_keeps_auth_independent(monkeypatch):
    from patchbay import server
    monkeypatch.setitem(server.config["server"], "allowed_origins", ["https://trusted.example"])
    monkeypatch.setattr(server, "auth_policy", AuthPolicy(enabled=True, token="fixture-bearer"))
    client = TestClient(server.app); before = dict(server.sessions)
    request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}}
    denied = client.post("/mcp", json=request, headers={"Origin": "https://evil.example", "Authorization": "Bearer fixture-bearer"})
    assert denied.status_code == 403 and server.sessions == before
    duplicated = client.post("/mcp", json=request, headers=[("Origin", "https://trusted.example"), ("Origin", "https://evil.example"), ("Authorization", "Bearer fixture-bearer")])
    assert duplicated.status_code == 403 and server.sessions == before
    wrong_token = client.post("/mcp", json=request, headers={"Origin": "https://trusted.example", "Authorization": "Bearer wrong"})
    assert wrong_token.status_code == 401 and server.sessions == before
    accepted = client.post("/mcp", json=request, headers={"Authorization": "Bearer fixture-bearer"})
    assert accepted.status_code == 200 and accepted.json()["result"]["protocolVersion"] == "2025-11-25"
    server.sessions.clear(); server.sessions.update(before)


@pytest.mark.parametrize("trigger", ["tunnel_exit", "quit", "interrupt"])
def test_every_coupled_launcher_stop_uses_common_server_budget(monkeypatch, trigger):
    from patchbay import cli
    class Process:
        def __init__(self, code=None): self.code = code
        def poll(self): return self.code
    server = Process(); tunnel = Process(1 if trigger == "tunnel_exit" else None)
    calls = []
    def terminate(process, **kwargs): calls.append((process, kwargs)); process.code = 0
    monkeypatch.setattr(cli, "terminate_process", terminate)
    monkeypatch.setattr(cli, "_print_controls", lambda: None)
    monkeypatch.setattr(cli, "_read_control_key", lambda: "q" if trigger == "quit" else None)
    def interrupt(*args): raise KeyboardInterrupt()
    monkeypatch.setattr(cli.time, "sleep", interrupt)
    cli._wait_supervised(server, tunnel, server_url="http://fixture")
    assert [kw for process, kw in calls if process is server] == [{"timeout_seconds": 45.0}]


def test_hub_authoritative_connection_uses_full_sync(tmp_path):
    from patchbay.hub.store_v2 import HubStoreV2
    store = HubStoreV2(tmp_path / "hub.sqlite3")
    try:
        assert store._connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert store._connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally: store.close()


def test_child_refuses_changed_launch_configuration_before_runtime_init(tmp_path):
    import os
    import subprocess
    import sys
    config = tmp_path / "config.yaml"; config.write_text("deliberately: incomplete\n")
    result = subprocess.run([sys.executable, "-c", "import patchbay.server"],
        env={**os.environ, "PATCHBAY_CONFIG": str(config), "PATCHBAY_CONFIG_SHA256": "f" * 64},
        capture_output=True, text=True, timeout=20)
    assert result.returncode != 0 and "Launch configuration identity changed" in result.stderr
    assert not (tmp_path / "logs").exists()
