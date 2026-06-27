import pytest

from power_tools import PowerToolRunner
from workspace_context import WorkspaceContext


def make_config(root, power=None):
    return {
        "repositories": {"default": str(root), "allowed": [str(root)]},
        "security": {
            "allowed_env_keys": ["PATH"],
            "blocked_globs": [".env", ".git", ".git/**", "**/.git/**"],
        },
        "power_tools": {
            "direct_write": False,
            "bash_mode": "off",
            "bash_transcript": "compact",
            "bash_session_id": "",
            "require_bash_session": False,
            "bash_timeout_ms": 30_000,
            "bash_max_output_bytes": 20_000,
            **(power or {}),
        },
    }


@pytest.mark.asyncio
async def test_run_command_disabled_by_default(tmp_path):
    runner = PowerToolRunner(make_config(tmp_path), WorkspaceContext(make_config(tmp_path)))

    with pytest.raises(ValueError, match="disabled"):
        await runner.run_command({"command": "pwd"})


@pytest.mark.asyncio
async def test_safe_command_allows_pwd_and_blocks_file_reader(tmp_path):
    config = make_config(tmp_path, {"bash_mode": "safe"})
    runner = PowerToolRunner(config, WorkspaceContext(config))

    result = await runner.run_command({"command": "pwd"})
    assert result["exit_code"] == 0
    assert result["bash_mode"] == "safe"
    assert result["cwd"] == "."

    with pytest.raises(ValueError, match="blocked"):
        await runner.run_command({"command": "cat README.md"})


@pytest.mark.asyncio
async def test_safe_command_blocks_home_expansion(tmp_path):
    config = make_config(tmp_path, {"bash_mode": "safe"})
    runner = PowerToolRunner(config, WorkspaceContext(config))

    with pytest.raises(ValueError, match="blocked"):
        await runner.run_command({"command": "ls $HOME"})


@pytest.mark.asyncio
async def test_full_command_can_run_custom_command_in_workspace(tmp_path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    config = make_config(tmp_path, {"bash_mode": "full"})
    runner = PowerToolRunner(config, WorkspaceContext(config))

    result = await runner.run_command({"command": "printf 123"})

    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "123"
    assert result["bash_mode"] == "full"


@pytest.mark.asyncio
async def test_star_allowed_env_inherits_full_environment(monkeypatch, tmp_path):
    config = make_config(tmp_path, {"bash_mode": "full"})
    config["security"]["allowed_env_keys"] = ["*"]
    monkeypatch.setenv("CODEX_MCP_WRAPPER_TEST_ENV", "visible")
    runner = PowerToolRunner(config, WorkspaceContext(config))

    result = await runner.run_command({"command": "printf '%s' \"$CODEX_MCP_WRAPPER_TEST_ENV\""})

    assert result["exit_code"] == 0
    assert result["stdout"] == "visible"


@pytest.mark.asyncio
async def test_required_bash_session_is_enforced(tmp_path):
    config = make_config(
        tmp_path,
        {"bash_mode": "safe", "bash_session_id": "main", "require_bash_session": True},
    )
    runner = PowerToolRunner(config, WorkspaceContext(config))

    with pytest.raises(ValueError, match="session id is required"):
        await runner.run_command({"command": "pwd"})

    result = await runner.run_command({"command": "pwd", "session_id": "main"})
    assert result["exit_code"] == 0
    assert result["bash_session_id"] == "main"
