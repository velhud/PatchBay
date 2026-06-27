import asyncio

import pytest

from patchbay.jobs.executor import JobExecutor
from patchbay.jobs.manager import JobManager, JobState
from patchbay.ownership import OWNER_CLIENT_REF_OPTION, OWNER_SESSION_HASH_OPTION
from patchbay.protocol.context import RequestContext
from patchbay.tools.handler import ToolHandler


def make_config(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    return {
        "server": {
            "max_concurrent_jobs": 3,
            "job_timeout_seconds": 30,
            "job_cleanup_after_hours": 24,
        },
        "repositories": {"default": str(repo), "allowed": [str(repo)]},
        "security": {
            "require_git_repo": False,
            "default_sandbox": "read-only",
            "allowed_env_keys": ["PATH"],
            "allowed_config_override_prefixes": [],
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
        "locks": {"root": str(tmp_path / "locks")},
    }


class RecordingExecutor(JobExecutor):
    def __init__(self, config, job_manager):
        super().__init__(config, job_manager)
        self.started = []

    async def execute_job(self, job_id):
        self.started.append(job_id)


@pytest.mark.asyncio
async def test_interactive_starts_durable_async_job(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(config, manager)
    handler = ToolHandler(config, manager, executor)

    result = await handler.handle_tool_call("codex_interactive", {"prompt": "inspect", "sandbox": "workspace-write"})
    await asyncio.sleep(0)

    job = manager.get_job(result["job_id"])
    assert job is not None
    assert job.mode == "interactive"
    assert job.prompt == "inspect"
    assert job.options["sandbox"] == "workspace-write"
    assert executor.started == [result["job_id"]]


@pytest.mark.asyncio
async def test_low_level_job_stores_private_owner_metadata_when_context_is_available(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(config, manager)
    handler = ToolHandler(config, manager, executor)
    context = RequestContext(transport_session_id="session-a", client_ref="client_a", client_label="Chat A")

    result = await handler.handle_tool_call(
        "codex_interactive",
        {"prompt": "inspect"},
        context=context,
    )
    await asyncio.sleep(0)

    job = manager.get_job(result["job_id"])
    assert job.options[OWNER_SESSION_HASH_OPTION] == "client_a"
    assert job.options[OWNER_CLIENT_REF_OPTION] == "client_a"
    assert OWNER_SESSION_HASH_OPTION not in str(result)
    assert "client_a" not in str(result)


@pytest.mark.asyncio
async def test_low_level_base_writing_job_holds_repo_mutation_lock_until_cancelled(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(config, manager)
    handler = ToolHandler(config, manager, executor)

    first = await handler.handle_tool_call(
        "codex_interactive",
        {"prompt": "write directly", "sandbox": "workspace-write"},
    )
    second = await handler.handle_tool_call(
        "codex_interactive",
        {"prompt": "write directly too", "sandbox": "workspace-write"},
    )

    assert first["job_id"]
    assert second["repo_busy"] is True
    job = manager.get_job(first["job_id"])
    assert job.options["_repo_mutation_lock"] is True
    assert job.options["_repo_mutation_lock_operation"] == "codex_interactive"

    cancelled = await handler.handle_tool_call("codex_cancel_job", {"job_id": first["job_id"]})
    assert cancelled["cancelled"] is True

    third = await handler.handle_tool_call(
        "codex_interactive",
        {"prompt": "write after cancel", "sandbox": "workspace-write"},
    )
    assert third["job_id"]


@pytest.mark.asyncio
async def test_resume_starts_durable_async_job_with_session_id(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = RecordingExecutor(config, manager)
    handler = ToolHandler(config, manager, executor)

    result = await handler.handle_tool_call("codex_resume", {"session_id": "session-123", "prompt": "continue"})
    await asyncio.sleep(0)

    job = manager.get_job(result["job_id"])
    assert job is not None
    assert job.mode == "resume"
    assert job.prompt == "continue"
    assert job.options["resume_session_id"] == "session-123"
    assert result["session_id"] == "session-123"
    assert executor.started == [result["job_id"]]


@pytest.mark.asyncio
async def test_interactive_reply_uses_repo_from_prior_session_job(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    prior_job_id = manager.create_job("interactive", "start", config["repositories"]["default"], {})
    manager.update_job_state(
        prior_job_id,
        JobState.COMPLETED,
        result={"summary": "done"},
        session_id="session-123",
        exit_code=0,
    )
    executor = RecordingExecutor(config, manager)
    handler = ToolHandler(config, manager, executor)

    result = await handler.handle_tool_call("codex_interactive_reply", {"session_id": "session-123", "prompt": "next"})
    await asyncio.sleep(0)

    job = manager.get_job(result["job_id"])
    assert job is not None
    assert job.mode == "resume"
    assert job.repo_path == config["repositories"]["default"]
    assert job.options["resume_session_id"] == "session-123"


@pytest.mark.asyncio
async def test_resume_skips_out_of_scope_stale_session_jobs(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    stale_repo = tmp_path / "stale-repo"
    stale_repo.mkdir()
    config["repositories"]["allowed"].append(str(stale_repo))
    stale_job_id = manager.create_job("interactive", "old", str(stale_repo), {})
    manager.update_job_state(
        stale_job_id,
        JobState.COMPLETED,
        result={"summary": "stale"},
        session_id="session-123",
        exit_code=0,
    )
    config["repositories"]["allowed"] = [config["repositories"]["default"]]
    current_job_id = manager.create_job("interactive", "current", config["repositories"]["default"], {})
    manager.update_job_state(
        current_job_id,
        JobState.COMPLETED,
        result={"summary": "current"},
        session_id="session-123",
        exit_code=0,
    )
    executor = RecordingExecutor(config, manager)
    handler = ToolHandler(config, manager, executor)

    result = await handler.handle_tool_call("codex_resume", {"session_id": "session-123", "prompt": "next"})
    await asyncio.sleep(0)

    job = manager.get_job(result["job_id"])
    assert job is not None
    assert job.mode == "resume"
    assert job.repo_path == config["repositories"]["default"]


@pytest.mark.asyncio
async def test_list_sessions_returns_metadata_without_transcripts_or_repo_paths(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    job_id = manager.create_job("interactive", "prompt must not return", config["repositories"]["default"], {})
    manager.update_job_state(
        job_id,
        JobState.COMPLETED,
        result={"summary": "done token=fixture-value", "files_changed": ["README.md"]},
        session_id="session-123",
        exit_code=0,
    )
    executor = RecordingExecutor(config, manager)
    handler = ToolHandler(config, manager, executor)

    result = await handler.handle_tool_call("codex_list_sessions", {})

    assert result["count"] == 1
    assert result["total_known"] == 1
    assert result["transcripts_returned"] is False
    assert result["repo_paths_returned"] is False
    assert result["sessions"][0]["session_id"] == "session-123"
    assert result["sessions"][0]["last_job_id"] == job_id
    assert result["sessions"][0]["summary"] == "done token=[REDACTED_POSSIBLE_SECRET]"
    assert result["sessions"][0]["files_changed"] == ["README.md"]
    assert "prompt must not return" not in str(result)
    assert config["repositories"]["default"] not in str(result)


@pytest.mark.asyncio
async def test_list_sessions_works_after_durable_reload_and_deduplicates(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    first_job_id = manager.create_job("interactive", "start", config["repositories"]["default"], {})
    manager.update_job_state(
        first_job_id,
        JobState.COMPLETED,
        result={"summary": "old", "files_changed": []},
        session_id="session-123",
        exit_code=0,
    )
    second_job_id = manager.create_job("resume", "continue", config["repositories"]["default"], {"resume_session_id": "session-123"})
    manager.update_job_state(
        second_job_id,
        JobState.COMPLETED,
        result={"summary": "new", "files_changed": ["app.py"]},
        session_id="session-123",
        exit_code=0,
    )

    reloaded = JobManager(config)
    executor = RecordingExecutor(config, reloaded)
    handler = ToolHandler(config, reloaded, executor)

    result = await handler.handle_tool_call("codex_list_sessions", {"max_sessions": 10})

    assert result["count"] == 1
    assert result["sessions"][0]["session_id"] == "session-123"
    assert result["sessions"][0]["last_job_id"] == second_job_id
    assert result["sessions"][0]["summary"] == "new"
