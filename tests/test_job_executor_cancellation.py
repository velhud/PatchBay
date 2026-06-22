import asyncio
import sys

import pytest

from job_executor import JobExecutor
from job_manager import JobManager, JobState


def make_config(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    return {
        "server": {
            "max_concurrent_jobs": 1,
            "job_timeout_seconds": 30,
            "job_cleanup_after_hours": 24,
        },
        "repositories": {"default": str(repo), "allowed": [str(repo)]},
        "security": {
            "require_git_repo": False,
            "default_sandbox": "read-only",
            "allowed_env_keys": ["PATH", "PYTHONPATH"],
        },
        "logging": {
            "job_logs_dir": str(tmp_path / "logs" / "jobs"),
            "job_state_dir": str(tmp_path / "logs" / "jobs" / "state"),
        },
    }


@pytest.mark.asyncio
async def test_cancel_job_terminates_running_process(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)
    job_id = manager.create_job("plan", "sleep", config["repositories"]["default"], {})

    def fake_command(mode, prompt, cwd, options=None):
        return [sys.executable, "-c", "import time; time.sleep(30)"]

    monkeypatch.setattr(executor, "_build_codex_command", fake_command)

    task = asyncio.create_task(executor.execute_job(job_id))
    for _ in range(50):
        if job_id in executor.processes:
            break
        await asyncio.sleep(0.02)

    result = await executor.cancel_job(job_id)
    await asyncio.wait_for(task, timeout=5)

    job = manager.get_job(job_id)
    assert result["cancelled"] is True
    assert result["process_signalled"] is True
    assert job.state == JobState.CANCELLED
    assert job.error == "Cancelled by request"
    assert job_id not in executor.processes


@pytest.mark.asyncio
async def test_cancel_unknown_job_returns_false(tmp_path):
    config = make_config(tmp_path)
    manager = JobManager(config)
    executor = JobExecutor(config, manager)

    result = await executor.cancel_job("missing")

    assert result["cancelled"] is False
    assert "Unknown job" in result["reason"]
