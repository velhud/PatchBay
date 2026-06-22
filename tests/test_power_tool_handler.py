import pytest

from job_executor import JobExecutor
from job_manager import JobManager
from tools import ToolHandler


def make_config(tmp_path, power=None):
    return {
        "server": {
            "max_concurrent_jobs": 1,
            "job_timeout_seconds": 30,
            "job_cleanup_after_hours": 24,
        },
        "repositories": {"default": str(tmp_path), "allowed": [str(tmp_path)]},
        "security": {
            "require_git_repo": False,
            "default_sandbox": "read-only",
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
        "logging": {
            "job_logs_dir": str(tmp_path / "logs" / "jobs"),
            "job_state_dir": str(tmp_path / "logs" / "jobs" / "state"),
        },
    }


@pytest.mark.asyncio
async def test_public_power_tools_deny_by_default(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    handler = ToolHandler(config, manager, JobExecutor(config, manager))

    with pytest.raises(ValueError, match="codex_write_file is disabled"):
        await handler.handle_tool_call("codex_write_file", {"file_path": "x.txt", "content": "hello"})

    with pytest.raises(ValueError, match="codex_run_command is disabled"):
        await handler.handle_tool_call("codex_run_command", {"command": "pwd"})


@pytest.mark.asyncio
async def test_public_power_tools_work_when_enabled(tmp_path):
    config = make_config(tmp_path, {"direct_write": True, "bash_mode": "safe"})
    manager = JobManager(config)
    handler = ToolHandler(config, manager, JobExecutor(config, manager))

    written = await handler.handle_tool_call("codex_write_file", {"file_path": "x.txt", "content": "hello\n"})
    edited = await handler.handle_tool_call("codex_edit_file", {"file_path": "x.txt", "old_text": "hello", "new_text": "hi"})
    command = await handler.handle_tool_call("codex_run_command", {"command": "pwd"})

    assert written["path"] == "x.txt"
    assert edited["replacements"] == 1
    assert command["exit_code"] == 0
