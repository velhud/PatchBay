import json

import pytest

from patchbay.jobs.executor import JobExecutor
from patchbay.jobs.manager import JobManager
from patchbay.protocol.context import RequestContext
from patchbay.tools.handler import ToolHandler


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
        "locks": {"root": str(tmp_path / "locks")},
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


@pytest.mark.asyncio
async def test_direct_write_and_command_refuse_when_repo_mutation_lock_is_busy(tmp_path):
    config = make_config(tmp_path, {"direct_write": True, "bash_mode": "safe"})
    manager = JobManager(config)
    handler = ToolHandler(config, manager, JobExecutor(config, manager))
    lease = await handler.repo_locks.acquire(str(tmp_path), operation="test_holder")
    try:
        written = await handler.handle_tool_call("codex_write_file", {"file_path": "x.txt", "content": "hello\n"})
        command = await handler.handle_tool_call("codex_run_command", {"command": "pwd"})
    finally:
        lease.release()

    assert written["repo_busy"] is True
    assert command["repo_busy"] is True
    assert not (tmp_path / "x.txt").exists()
    assert str(tmp_path) not in str(written)
    assert str(tmp_path) not in str(command)


@pytest.mark.asyncio
async def test_self_test_includes_shared_server_coordination_without_raw_session(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    handler = ToolHandler(config, manager, JobExecutor(config, manager))
    context = RequestContext.from_session(
        "private-session-id",
        {"tool_mode": "worker", "client_label": "planning"},
        salt="test-salt",
        active_mcp_sessions=2,
    )

    result = await handler.handle_tool_call("codex_self_test", {}, context=context)

    coordination = result["coordination"]
    assert coordination["shared_server"] is True
    assert coordination["client_ref"].startswith("client_")
    assert coordination["client"]["tool_mode"] == "worker"
    assert coordination["client"]["client_label"] == "planning"
    assert coordination["active_mcp_sessions"] == 2
    assert "transport sessions" in coordination["note"]
    assert "not this count by itself" in coordination["note"]
    assert coordination["raw_session_ids_returned"] is False
    assert "private-session-id" not in json.dumps(result)
