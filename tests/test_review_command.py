import pytest

from patchbay.jobs.executor import JobExecutor
from patchbay.jobs.manager import JobManager
from patchbay.tools.handler import ToolHandler


def make_handler(tmp_path, allowed_config_override_prefixes=None):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    config = {
        "server": {
            "max_concurrent_jobs": 1,
            "job_timeout_seconds": 30,
            "job_cleanup_after_hours": 24,
        },
        "repositories": {"default": str(repo), "allowed": [str(repo)]},
        "security": {
            "require_git_repo": False,
            "default_sandbox": "read-only",
            "allowed_env_keys": ["PATH"],
            "allowed_config_override_prefixes": allowed_config_override_prefixes or [],
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
        },
        "logging": {
            "job_logs_dir": str(tmp_path / "logs" / "jobs"),
            "job_state_dir": str(tmp_path / "logs" / "jobs" / "state"),
        },
    }
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    return ToolHandler(config, manager, executor)


def test_review_command_keeps_prompt_on_stdin(tmp_path):
    handler = make_handler(tmp_path)

    cmd, stdin_data = handler._build_review_command(
        {
            "prompt": "review these changes",
            "base": "main",
            "title": "Review title",
            "model": "gpt-5",
        }
    )

    assert cmd[:2] == ["codex", "review"]
    assert "--disable" not in cmd
    assert cmd[-1] == "-"
    assert "review these changes" not in cmd
    assert stdin_data == b"review these changes"
    assert cmd.index("--base") < len(cmd) - 1
    assert cmd.index("--title") < len(cmd) - 1
    assert "-c" in cmd


def test_review_command_requires_allowed_config_override_prefix(tmp_path):
    handler = make_handler(tmp_path)

    with pytest.raises(ValueError, match="config_overrides are disabled"):
        handler._build_review_command({"config_overrides": ["features.foo=true"]})

    allowed = make_handler(tmp_path / "allowed", allowed_config_override_prefixes=["features."])
    cmd, stdin_data = allowed._build_review_command({"config_overrides": ["features.foo=true"]})

    assert stdin_data is None
    assert cmd[-2:] == ["-c", "features.foo=true"]


def test_review_command_rejects_uncommitted_with_base_or_commit(tmp_path):
    handler = make_handler(tmp_path)

    with pytest.raises(ValueError, match="either uncommitted=true or base/commit"):
        handler._build_review_command({"uncommitted": True, "base": "main"})

    with pytest.raises(ValueError, match="either uncommitted=true or base/commit"):
        handler._build_review_command({"uncommitted": True, "commit": "abc123"})
