import pytest

from patchbay.jobs.executor import JobExecutor
from patchbay.jobs.manager import JobManager
from patchbay.tools.handler import ToolHandler


def make_config(tmp_path, security=None):
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
            "allowed_config_override_prefixes": [],
            "blocked_globs": [".env", ".git", ".git/**", "**/.git/**"],
            **(security or {}),
        },
        "power_tools": {
            "direct_write": False,
            "bash_mode": "off",
            "bash_transcript": "compact",
            "bash_session_id": "",
            "require_bash_session": False,
            "bash_timeout_ms": 30_000,
            "bash_max_output_bytes": 20_000,
        },
        "logging": {
            "job_logs_dir": str(tmp_path / "logs" / "jobs"),
            "job_state_dir": str(tmp_path / "logs" / "jobs" / "state"),
        },
    }


def make_handler(tmp_path, security=None):
    config = make_config(tmp_path, security=security)
    manager = JobManager(config)
    return ToolHandler(config, manager, JobExecutor(config, manager))


@pytest.mark.asyncio
async def test_hidden_experimental_handlers_are_not_default_dispatch_targets(tmp_path):
    handler = make_handler(tmp_path)

    for tool_name in [
        "codex_apply_diff",
        "codex_cloud_exec",
        "codex_cloud_status",
        "codex_cloud_diff",
        "string_transform",
        "codex_sandbox",
    ]:
        with pytest.raises(ValueError, match="Unknown tool"):
            await handler.handle_tool_call(tool_name, {})


@pytest.mark.asyncio
async def test_removed_sandbox_handler_stays_unavailable_even_with_legacy_flag(tmp_path):
    handler = make_handler(tmp_path, security={"expose_codex_sandbox_tool": True})

    with pytest.raises(ValueError, match="Unknown tool"):
        await handler.handle_tool_call("codex_sandbox", {"command": "pwd"})

    assert not hasattr(handler, "_codex_sandbox")
