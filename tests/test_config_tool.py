import asyncio
import json
from pathlib import Path

import pytest

from patchbay.tools.handler import ToolHandler


class DummyJobManager:
    pass


class DummyJobExecutor:
    pass


class FakeProcess:
    returncode = 0

    async def communicate(self):
        return b"json_events stable true\n", b""


@pytest.mark.asyncio
async def test_codex_get_config_never_returns_raw_local_config(monkeypatch, tmp_path):
    codex_home = tmp_path / "home"
    codex_config = codex_home / ".codex" / "config.toml"
    codex_config.parent.mkdir(parents=True)
    codex_config.write_text(
        'model = "gpt-5"\n'
        'api_key = "dummy-secret-value-that-should-not-return"\n'
        'projects = "/example/project/path"\n',
        encoding="utf-8",
    )

    async def fake_create_subprocess_exec(*args, **kwargs):
        assert args == ("codex", "features", "list")
        return FakeProcess()

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: codex_home))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    handler = ToolHandler(
        {
            "server": {"host": "127.0.0.1", "port": 8000, "enable_cors": False},
            "app": {"tool_mode": "worker"},
            "repositories": {"default": ".", "allowed": ["."]},
            "security": {
                "default_sandbox": "read-only",
                "allow_dangerously_bypass": False,
                "allowed_config_override_prefixes": [],
                "allowed_env_keys": ["PATH", "OPENAI_API_KEY"],
            },
        },
        DummyJobManager(),
        DummyJobExecutor(),
    )

    result = await handler._codex_get_config({})
    serialized = json.dumps(result)

    assert result["codex_config"]["present"] is True
    assert result["codex_config"]["raw_values_returned"] is False
    assert "config" not in result
    assert "dummy-secret-value-that-should-not-return" not in serialized
    assert "/example/project/path" not in serialized
    assert result["patchbay_config"]["power_tools"]["direct_write"] is False
    assert result["patchbay_config"]["tool_mode"] == "worker"
    assert result["patchbay_config"]["power_tools"]["bash_mode"] == "off"
    assert result["patchbay_config"]["power_tools"]["codex_session_read"] is False
    assert result["patchbay_config"]["power_tools"]["codex_home_configured"] is False
    assert "sandbox_tool_exposed" not in result["patchbay_config"]
    assert result["capabilities"] == {"json_events": {"stage": "stable", "enabled": True}}


@pytest.mark.asyncio
async def test_codex_get_config_uses_configured_codex_home(monkeypatch, tmp_path):
    codex_home = tmp_path / "configured-codex"
    codex_config = codex_home / "config.toml"
    codex_config.parent.mkdir(parents=True)
    codex_config.write_text('model = "gpt-5"\n', encoding="utf-8")

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    handler = ToolHandler(
        {
            "server": {"host": "127.0.0.1", "port": 8000, "enable_cors": False},
            "app": {"tool_mode": "worker"},
            "repositories": {"default": ".", "allowed": ["."]},
            "security": {
                "default_sandbox": "read-only",
                "allow_dangerously_bypass": False,
                "allowed_config_override_prefixes": [],
                "allowed_env_keys": ["PATH"],
            },
            "power_tools": {"codex_home": str(codex_home)},
        },
        DummyJobManager(),
        DummyJobExecutor(),
    )

    result = await handler._codex_get_config({})

    assert result["codex_config"]["present"] is True
    assert result["codex_config"]["path_hint"] == "configured_codex_home/config.toml"
    assert result["patchbay_config"]["power_tools"]["codex_home_configured"] is True


@pytest.mark.asyncio
async def test_codex_get_config_hides_feature_list_stderr(monkeypatch, tmp_path):
    class FailingProcess:
        returncode = 2

        async def communicate(self):
            return b"", b"local secret path /example/project/path"

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FailingProcess()

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    handler = ToolHandler(
        {
            "server": {"host": "127.0.0.1", "port": 8000, "enable_cors": False},
            "repositories": {"default": ".", "allowed": ["."]},
            "security": {"default_sandbox": "read-only"},
        },
        DummyJobManager(),
        DummyJobExecutor(),
    )

    result = await handler._codex_get_config({})
    serialized = json.dumps(result)

    assert result["capabilities_error"] == {
        "message": "Unable to list Codex features.",
        "exit_code": 2,
    }
    assert "/example/project/path" not in serialized


@pytest.mark.asyncio
async def test_codex_get_config_reports_auth_without_returning_token(monkeypatch, tmp_path):
    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setenv("PATCHBAY_HTTP_TOKEN", "auth-fixture-value-not-returned")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    handler = ToolHandler(
        {
            "server": {"host": "127.0.0.1", "port": 8000, "enable_cors": False},
            "auth": {"token_env": "PATCHBAY_HTTP_TOKEN", "allow_query_token": True},
            "repositories": {"default": ".", "allowed": ["."]},
            "security": {"default_sandbox": "read-only"},
            "logging": {"access_log": False},
        },
        DummyJobManager(),
        DummyJobExecutor(),
    )

    result = await handler._codex_get_config({})
    serialized = json.dumps(result)

    assert result["patchbay_config"]["http_auth"]["enabled"] is True
    assert result["patchbay_config"]["http_auth"]["token_configured"] is True
    assert result["patchbay_config"]["http_auth"]["token_returned"] is False
    assert "auth-fixture-value-not-returned" not in serialized
